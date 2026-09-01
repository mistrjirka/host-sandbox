from __future__ import annotations

import concurrent.futures
import hashlib
import json
import threading
import time
from urllib.parse import quote
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from . import __version__

from .ssh_router import SSHRouter


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        out["required"] = required
    return out


def _session_schema(extra: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    props = {"session_id": {"type": "string", "pattern": "^s_[a-f0-9]{16}$"}}
    props.update(extra or {})
    return _obj(props, ["session_id", *(required or [])])


def _display_title(name: str) -> str:
    return " ".join(part.capitalize() for part in name.split("_"))


def _normalize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize equivalent JSON Schema spellings to MCP SDK/Pydantic style."""
    types = schema.get("type")
    if isinstance(types, list):
        extras = {k: v for k, v in schema.items() if k != "type"}
        branches: list[dict[str, Any]] = []
        for typ in types:
            branch: dict[str, Any] = {"type": typ}
            if typ == "object" and "additionalProperties" in extras:
                branch["additionalProperties"] = extras["additionalProperties"]
            if typ == "array" and "items" in extras:
                branch["items"] = extras["items"]
            branches.append(branch)
        schema.clear()
        schema.update({k: v for k, v in extras.items() if k not in {"additionalProperties", "items"}})
        schema["anyOf"] = branches

    for key, value in list(schema.items()):
        if isinstance(value, dict):
            _normalize_schema(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _normalize_schema(item)
    return schema


def _add_schema_titles(schema: dict[str, Any], *, root_title: str | None = None) -> dict[str, Any]:
    """Make hand-written schemas resemble MCP SDK/Pydantic-generated schemas."""
    _normalize_schema(schema)
    if root_title and "title" not in schema:
        schema["title"] = root_title
    props = schema.get("properties")
    if isinstance(props, dict):
        for key, value in props.items():
            if not isinstance(value, dict):
                continue
            value.setdefault("title", _display_title(key))
            _add_schema_titles(value)
    items = schema.get("items")
    if isinstance(items, dict):
        _add_schema_titles(items)
    for key in ("anyOf", "oneOf", "allOf"):
        choices = schema.get(key)
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict):
                    _add_schema_titles(choice)
    return schema


_TOOL_TITLES = {
    "sandbox_health": "Sandbox health",
    "list_projects": "List sandbox projects",
    "list_sessions": "List development sessions",
    "create_session": "Create development session",
    "destroy_session": "Destroy development session",
    "path_info": "Inspect path",
    "list_repositories": "List repositories",
    "exec_command": "Execute command",
    "exec_commands": "Execute commands",
    "list_jobs": "List command jobs",
    "get_job": "Get command job",
    "delete_job": "Delete command job",
    "cleanup_jobs": "Cleanup command jobs",
    "terminate_job": "Terminate command job",
    "search_project": "Search repository files",
    "search_many": "Search many repository queries",
    "read_file": "Read repository file",
    "read_files": "Read repository files",
    "write_file": "Write repository file",
    "replace_text": "Replace exact repository text",
    "read_binary_file": "Read binary file",
    "view_image": "View image",
    "write_binary_file": "Write binary file",
    "apply_patch": "Apply Git patch",
    "git_status_diff": "Inspect Git status and diff",
    "list_processes": "List host processes",
    "read_terminal": "Read interactive terminal",
    "send_terminal_input": "Send interactive terminal input",
    "send_terminal_key": "Send interactive terminal key",
    "signal_process": "Signal host process",
}

_READ_ONLY_TOOLS = {
    "sandbox_health", "list_projects", "list_sessions", "path_info",
    "list_repositories", "list_jobs", "get_job", "search_project", "search_many",
    "read_file", "read_files", "read_binary_file", "view_image", "git_status_diff",
    "list_processes", "read_terminal",
}
_OPEN_WORLD_TOOLS = {"exec_command", "exec_commands", "send_terminal_input"}
_DESTRUCTIVE_TOOLS = {
    "destroy_session", "exec_command", "exec_commands", "delete_job", "cleanup_jobs", "terminate_job",
    "write_file", "replace_text", "write_binary_file", "apply_patch", "signal_process",
    "send_terminal_input", "send_terminal_key",
}


def _decorate_router_tools(tools: list[dict[str, Any]]) -> None:
    for tool in tools:
        name = tool["name"]
        tool["title"] = _TOOL_TITLES.get(name, _display_title(name))
        _add_schema_titles(tool["inputSchema"], root_title=f"{name}Arguments")
        tool["outputSchema"] = {
            "additionalProperties": True,
            "title": f"{name}DictOutput",
            "type": "object",
        }
        read_only = name in _READ_ONLY_TOOLS
        tool["annotations"] = {
            "readOnlyHint": read_only,
            "destructiveHint": name in _DESTRUCTIVE_TOOLS,
            "idempotentHint": False,
            "openWorldHint": name in _OPEN_WORLD_TOOLS,
        }



ROUTER_TOOLS: list[dict[str, Any]] = [
    {"name": "sandbox_health", "description": "Check that the Host Sandbox router and configured computers are reachable.", "inputSchema": _obj({})},
    {"name": "list_projects", "description": "List configured computers. Each computer is exposed as a Development-Sandbox-style project.", "inputSchema": _obj({})},
    {"name": "list_sessions", "description": "List logical host sessions. A session selects one computer but does not create a container.", "inputSchema": _obj({"active_only": {"type": "boolean", "default": False}})},
    {"name": "create_session", "description": "Create a logical session for a configured computer. No container is created; tools run directly on that host OS.", "inputSchema": _obj({"project": {"type": "string"}, "label": {"type": ["string", "null"], "maxLength": 100}}, ["project"])},
    {"name": "destroy_session", "description": "Remove a logical host session. Files and processes on the computer are not deleted.", "inputSchema": _obj({"session_id": {"type": "string"}}, ["session_id"])},
    {"name": "path_info", "description": "Inspect a file or directory on the selected computer.", "inputSchema": _session_schema({"repo": {"type": "string", "default": "."}, "path": {"type": "string", "default": "."}, "include_hidden": {"type": "boolean", "default": True}, "max_entries": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 2000}})},
    {"name": "list_repositories", "description": "Discover Git repositories below the selected computer's workspace root.", "inputSchema": _session_schema({"max_depth": {"type": "integer", "minimum": 1, "maximum": 8, "default": 3}, "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1000}})},
    {"name": "exec_command", "description": "Run an unrestricted command directly on the selected computer.", "inputSchema": _session_schema({"command": {"type": "string", "maxLength": 1000000}, "cwd": {"type": "string", "default": "."}, "env": {"type": ["object", "null"], "additionalProperties": {"type": "string"}}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 604800, "default": 3600}, "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 20, "default": 8}, "max_output_bytes": {"type": "integer", "minimum": 1000, "maximum": 2097152, "default": 131072}, "resource_locks": {"type": "array", "items": {"type": "string"}, "maxItems": 16, "default": []}}, ["command"])},
    {"name": "exec_commands", "description": "Run independent commands on one or more selected computers concurrently.", "inputSchema": _obj({"commands": {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "object", "properties": {"session_id": {"type": "string"}, "command": {"type": "string"}, "cwd": {"type": "string", "default": "."}, "env": {"type": ["object", "null"], "additionalProperties": {"type": "string"}}, "timeout_seconds": {"type": "integer", "default": 3600}, "wait_seconds": {"type": "integer", "default": 8}, "max_output_bytes": {"type": "integer", "default": 131072}, "resource_locks": {"type": "array", "items": {"type": "string"}, "maxItems": 16}}, "required": ["session_id", "command"]}}, "concurrency": {"type": "integer", "minimum": 1, "maximum": 32, "default": 16}}, ["commands"])},
    {"name": "list_jobs", "description": "List recent command jobs for a host session.", "inputSchema": _session_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 50}})},
    {"name": "get_job", "description": "Read a host command job and its paginated output.", "inputSchema": _session_schema({"job_id": {"type": "string"}, "stdout_offset": {"type": "integer", "minimum": 0, "default": 0}, "stderr_offset": {"type": "integer", "minimum": 0, "default": 0}, "max_bytes": {"type": "integer", "minimum": 1000, "maximum": 2097152, "default": 131072}}, ["job_id"])},
    {"name": "delete_job", "description": "Delete durable stdout/stderr/state for a finished job.", "inputSchema": _session_schema({"job_id": {"type": "string", "pattern": "^j_[a-f0-9]{16}$"}}, ["job_id"])},
    {"name": "cleanup_jobs", "description": "Preview or delete old completed job logs. Defaults to dry-run.", "inputSchema": _obj({"session_id": {"anyOf": [{"type": "string", "pattern": "^s_[a-f0-9]{16}$"}, {"type": "null"}], "default": None}, "older_than_seconds": {"type": "integer", "minimum": 0, "maximum": 31536000, "default": 604800}, "keep_recent_per_session": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 20}, "max_delete": {"type": "integer", "minimum": 1, "maximum": 100000, "default": 10000}, "dry_run": {"type": "boolean", "default": True}})},
    {"name": "terminate_job", "description": "Stop a running command job on a computer.", "inputSchema": _session_schema({"job_id": {"type": "string"}, "signal": {"type": "string", "enum": ["TERM", "INT", "KILL", "HUP"], "default": "TERM"}, "force_after_seconds": {"type": "integer", "minimum": 0, "maximum": 60, "default": 5}}, ["job_id"])},
    {"name": "search_project", "description": "Search text below a repository/path on a selected computer.", "inputSchema": _session_schema({"query": {"type": "string"}, "repo": {"type": "string", "default": "."}, "path": {"type": "string", "default": "."}, "glob": {"type": ["array", "null"], "items": {"type": "string"}}, "fixed_strings": {"type": "boolean", "default": False}, "max_results": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 500}}, ["query"])},
    {"name": "search_many", "description": "Run multiple independent ripgrep searches in one MCP round trip.", "inputSchema": _obj({"searches": {"type": "array", "minItems": 1, "maxItems": 16, "items": {"type": "object", "properties": {"session_id": {"type": "string", "pattern": "^s_[a-f0-9]{16}$"}, "query": {"type": "string", "minLength": 1, "maxLength": 100000}, "repo": {"type": "string", "default": ".", "maxLength": 4000}, "path": {"type": "string", "default": ".", "maxLength": 8000}, "glob": {"type": "array", "items": {"type": "string"}, "maxItems": 100}, "fixed_strings": {"type": "boolean", "default": False}, "max_results": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 500}}, "required": ["session_id", "query"]}}, "concurrency": {"type": "integer", "minimum": 1, "maximum": 16, "default": 8}}, ["searches"])},
    {"name": "read_file", "description": "Read a line-numbered text chunk from a selected computer.", "inputSchema": _session_schema({"path": {"type": "string"}, "repo": {"type": "string", "default": "."}, "start_line": {"type": "integer", "minimum": 1, "default": 1}, "max_lines": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 1000}}, ["path"])},
    {"name": "read_files", "description": "Read multiple file chunks concurrently in one MCP round trip.", "inputSchema": _obj({"files": {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "object", "properties": {"session_id": {"type": "string", "pattern": "^s_[a-f0-9]{16}$"}, "path": {"type": "string", "minLength": 1, "maxLength": 8000}, "repo": {"type": "string", "default": ".", "maxLength": 4000}, "start_line": {"type": "integer", "minimum": 1, "default": 1}, "max_lines": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 1000}}, "required": ["session_id", "path"]}}, "concurrency": {"type": "integer", "minimum": 1, "maximum": 32, "default": 16}}, ["files"])},
    {"name": "write_file", "description": "Create or replace a text file on a selected computer.", "inputSchema": _session_schema({"path": {"type": "string"}, "content": {"type": "string"}, "repo": {"type": "string", "default": "."}, "create_parents": {"type": "boolean", "default": True}, "overwrite": {"type": "boolean", "default": True}}, ["path", "content"])},
    {"name": "replace_text", "description": "Replace exact text in a file on a selected computer.", "inputSchema": _session_schema({"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "repo": {"type": "string", "default": "."}, "expected_occurrences": {"type": ["integer", "null"]}}, ["path", "old_text", "new_text"])},
    {"name": "apply_patch", "description": "Validate or apply a unified Git patch at an explicit repository root.", "inputSchema": _session_schema({"patch": {"type": "string", "minLength": 1, "maxLength": 8388608}, "repo": {"type": "string", "default": "."}, "check_only": {"type": "boolean", "default": False}}, ["patch"])},
    {"name": "git_status_diff", "description": "Return Git state under one total response budget; optionally suppress diffs/untracked data or filter paths.", "inputSchema": _session_schema({"repo": {"type": "string", "default": "."}, "max_total_chars": {"type": "integer", "minimum": 1000, "maximum": 2000000, "default": 200000}, "include_untracked": {"type": "boolean", "default": True}, "include_diff": {"type": "boolean", "default": True}, "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 200, "default": []}})},
    {"name": "read_binary_file", "description": "Return a repository file as a native MCP embedded binary resource.", "inputSchema": _session_schema({"path": {"type": "string", "minLength": 1, "maxLength": 8000}, "repo": {"type": "string", "default": "."}, "max_bytes": {"type": "integer", "minimum": 1, "maximum": 16777216, "default": 16777216}}, ["path"])},
    {"name": "view_image", "description": "Read an image from the host and return native MCP ImageContent.", "inputSchema": _session_schema({"path": {"type": "string", "minLength": 1, "maxLength": 8000}, "repo": {"type": "string", "default": "."}, "max_bytes": {"type": "integer", "minimum": 1, "maximum": 16777216, "default": 16777216}}, ["path"])},
    {"name": "write_binary_file", "description": "Write raw bytes supplied as base64 to a selected computer.", "inputSchema": _session_schema({"path": {"type": "string", "minLength": 1, "maxLength": 8000}, "content_base64": {"type": "string", "minLength": 1, "maxLength": 22500000}, "repo": {"type": "string", "default": "."}, "create_parents": {"type": "boolean", "default": True}, "overwrite": {"type": "boolean", "default": True}}, ["path", "content_base64"])},
    {"name": "list_processes", "description": "List processes on the selected computer.", "inputSchema": _session_schema({"max_processes": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 500}})},
    {"name": "signal_process", "description": "Signal a process on the selected computer.", "inputSchema": _session_schema({"pid": {"type": "integer", "minimum": 2}, "signal": {"type": "string", "enum": ["TERM", "INT", "KILL", "HUP", "CONT", "STOP"], "default": "TERM"}}, ["pid"])},
    {"name": "read_terminal", "description": "Read tmux state for interactive programs. Prefer exec_command for normal commands.", "inputSchema": _session_schema({"cursor": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}], "default": None}, "max_bytes": {"type": "integer", "minimum": 1000, "maximum": 2097152, "default": 131072}, "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 25, "default": 0}, "screen_lines": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 200}})},
    {"name": "send_terminal_input", "description": "Type into the persistent tmux terminal. Use only for genuinely interactive programs.", "inputSchema": _session_schema({"text": {"type": "string", "minLength": 1, "maxLength": 1000000}, "press_enter": {"type": "boolean", "default": True}}, ["text"])},
    {"name": "send_terminal_key", "description": "Send a control/navigation key to the persistent interactive terminal.", "inputSchema": _session_schema({"key": {"type": "string", "enum": ["C-c", "C-d", "C-z", "Enter", "Tab", "Escape", "Up", "Down", "Left", "Right", "BSpace"]}}, ["key"])},
]

_decorate_router_tools(ROUTER_TOOLS)


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
        self._resource_locks_guard = threading.RLock()
        self._resource_locks: dict[tuple[str, str], threading.Lock] = {}
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
        if not self.router.has_host(s.project):
            raise KeyError(f"session host is not connected/configured: {s.project}")
        return s

    @staticmethod
    def _payload(result: Any) -> Any:
        if isinstance(result, dict) and "structuredContent" in result:
            return result["structuredContent"]
        return result

    def _resource_lock_objects(self, sid: str, names: list[str] | None) -> list[threading.Lock]:
        s = self._session(sid)
        normalized = sorted({str(name) for name in (names or []) if str(name)})
        if len(normalized) > 16:
            raise ValueError("at most 16 resource locks are allowed")
        with self._resource_locks_guard:
            return [self._resource_locks.setdefault((s.project, name), threading.Lock()) for name in normalized]

    def _remote(self, sid: str, tool: str, args: dict[str, Any], resource_locks: list[str] | None = None) -> Any:
        locks = self._resource_lock_objects(sid, resource_locks)
        acquired: list[threading.Lock] = []
        try:
            for lock in locks:
                lock.acquire()
                acquired.append(lock)
            s = self._session(sid)
            return self._payload(self.router.call_tool(s.project, tool, args))
        finally:
            for lock in reversed(acquired):
                lock.release()

    @staticmethod
    def _join(repo: str = ".", path: str = ".") -> str:
        repo = repo or "."; path = path or "."
        if repo == ".": return path
        if path == ".": return repo
        return str(Path(repo) / path)

    @staticmethod
    def _batch_results(items: list[dict[str, Any]], fn: Any, concurrency: int) -> dict[str, Any]:
        def run(index_item: tuple[int, dict[str, Any]]) -> dict[str, Any]:
            index, item = index_item
            try:
                return {"index": index, "ok": True, "http_status": 200, "result": fn(dict(item)), "error": None}
            except Exception as exc:
                return {"index": index, "ok": False, "http_status": 500, "result": None, "error": f"{type(exc).__name__}: {exc}"}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            rows = list(pool.map(run, enumerate(items)))
        return {"results": rows, "count": len(rows)}

    @staticmethod
    def _structured_search(raw: dict[str, Any], *, session_id: str, repo: str, query: str, target: str) -> dict[str, Any]:
        matches = []
        root_text = str(raw.get("root") or "")
        for row in raw.get("results", []):
            text = str(row)
            parts = text.split(":", 3)
            if len(parts) < 4:
                continue
            raw_path, line, column, body = parts
            path = raw_path
            if root_text and path.startswith(root_text.rstrip("/") + "/"):
                rel = path[len(root_text.rstrip("/"))+1:]
                path = str(Path(target) / rel) if target not in {"", "."} else rel
            if repo not in {"", "."} and path.startswith(repo.rstrip("/") + "/"):
                path = path[len(repo.rstrip("/"))+1:]
            try:
                line_i = int(line); col_i = int(column)
            except ValueError:
                continue
            matches.append({"path": path, "line": line_i, "column": col_i, "text": body})
        return {
            "session_id": session_id, "repo": repo, "query": query,
            "match_count_returned": len(matches), "truncated": bool(raw.get("truncated", False)), "matches": matches,
        }

    @staticmethod
    def _resource_uri(sid: str, target: str) -> str:
        return f"host-sandbox://{sid}/{quote(target.lstrip('/'), safe='/')}"

    def call(self, name: str, a: dict[str, Any]) -> Any:
        if name == "sandbox_health":
            return {"ok": True, "router": "host-sandbox", **self.router.list_hosts()}
        if name == "list_projects":
            return {"projects": self.router.project_entries()}
        if name == "list_sessions":
            active_only = bool(a.get("active_only", False))
            with self._lock:
                rows = [asdict(s) for s in self._sessions.values() if not active_only or s.running]
            return {"sessions": rows}
        if name == "create_session":
            project = str(a["project"])
            if not self.router.has_host(project): raise KeyError(f"unknown or disconnected project/host: {project}")
            now = time.time(); s = LogicalSession(self._sid(project, now), project, a.get("label"), now)
            with self._lock: self._sessions[s.id] = s; self._save()
            return {"id": s.id, "project": project, "label": s.label, "running": True, "container": None, "host_mode": True}
        if name == "destroy_session":
            s = self._session(str(a["session_id"])); s.running = False
            with self._lock: self._save()
            return {"id": s.id, "project": s.project, "running": False}
        if name == "cleanup_jobs":
            sid_value = a.get("session_id")
            args = {
                "older_than_seconds": a.get("older_than_seconds", 604800),
                "keep_recent": a.get("keep_recent_per_session", 20),
                "max_delete": a.get("max_delete", 10000),
                "dry_run": a.get("dry_run", True),
            }
            if sid_value:
                return self._remote(str(sid_value), "cleanup_jobs", args)
            projects = [entry["id"] for entry in self.router.project_entries()]
            rows = []
            for project in projects:
                try:
                    result = self._payload(self.router.call_tool(project, "cleanup_jobs", args))
                    rows.append({"project": project, "ok": True, "result": result})
                except Exception as exc:
                    rows.append({"project": project, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return {"results": rows, "count": len(rows), "dry_run": bool(args["dry_run"])}
        if name == "search_many":
            items = list(a.get("searches") or [])
            if len(items) > 16: raise ValueError("at most 16 searches are allowed")
            return self._batch_results(items, lambda item: self.call("search_project", item), max(1, min(16, int(a.get("concurrency", 8)))))
        if name == "read_files":
            items = list(a.get("files") or [])
            if len(items) > 32: raise ValueError("at most 32 files are allowed")
            return self._batch_results(items, lambda item: self.call("read_file", item), max(1, min(32, int(a.get("concurrency", 16)))))
        if name == "exec_commands":
            commands = list(a.get("commands") or [])
            if not commands:
                return {"results": []}
            if len(commands) > 32:
                raise ValueError("at most 32 commands are allowed")
            concurrency = max(1, min(32, int(a.get("concurrency", 16))))

            def run_batch_item(raw: dict[str, Any]) -> Any:
                item = dict(raw)
                sid = str(item.pop("session_id"))
                locks = list(item.pop("resource_locks", []) or [])
                args = {k: item[k] for k in ("command", "cwd", "env", "timeout_seconds", "wait_seconds", "max_output_bytes") if k in item}
                return self._remote(sid, "exec_command", args, locks)

            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [pool.submit(run_batch_item, item) for item in commands]
                return {"results": [future.result() for future in futures]}

        sid = str(a.pop("session_id"))
        if name == "exec_command":
            locks = list(a.pop("resource_locks", []) or [])
            args = {k: a[k] for k in ("command","cwd","env","timeout_seconds","wait_seconds","max_output_bytes") if k in a}
            return self._remote(sid, "exec_command", args, locks)
        if name == "list_jobs":
            return self._remote(sid, "list_jobs", {"limit": a.get("limit", 50)})
        if name == "get_job":
            return self._remote(sid, "read_job", {"job_id": a["job_id"], "stdout_offset": a.get("stdout_offset", 0), "stderr_offset": a.get("stderr_offset", 0), "max_bytes": a.get("max_bytes", 131072)})
        if name == "delete_job":
            return self._remote(sid, "delete_job", {"job_id": a["job_id"]})
        if name == "terminate_job":
            return self._remote(sid, "signal_job", {"job_id": a["job_id"], "sig": a.get("signal", "TERM"), "force_after_seconds": a.get("force_after_seconds", 5)})
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
            repo = a.get("repo", "."); rel_path = a.get("path", ".")
            target = self._join(repo, rel_path)
            raw = self._remote(sid, "search_text", {"query": a["query"], "path": target, "max_results": a.get("max_results",500), "fixed_strings": a.get("fixed_strings",False), "glob": a.get("glob")})
            return self._structured_search(raw, session_id=sid, repo=repo, query=a["query"], target=rel_path)
        if name == "read_file":
            repo = a.get("repo", "."); target = self._join(repo, a["path"])
            raw = self._remote(sid, "read_text_lines", {"path": target, "start_line": a.get("start_line",1), "max_lines": a.get("max_lines",1000)})
            raw["repo"] = repo; raw["path"] = a["path"]
            return raw
        if name == "write_file":
            target = self._join(a.get("repo", "."), a["path"])
            mode = "overwrite" if a.get("overwrite",True) else "exclusive"
            return self._remote(sid, "write_file", {"path": target, "content": a["content"], "mode": mode, "create_parents": a.get("create_parents",True)})
        if name == "replace_text":
            target = self._join(a.get("repo", "."), a["path"])
            return self._remote(sid, "replace_text", {"path": target, "old": a["old_text"], "new": a["new_text"], "expected_matches": a.get("expected_occurrences")})
        if name == "apply_patch":
            return self._remote(sid, "apply_patch", {"repo": a.get("repo", "."), "patch": a["patch"], "check_only": a.get("check_only", False)})
        if name == "git_status_diff":
            raw = self._remote(sid, "git_status_diff", {"repo": a.get("repo", "."), "max_total_chars": a.get("max_total_chars",200000), "include_untracked": a.get("include_untracked",True), "include_diff": a.get("include_diff",True), "paths": a.get("paths",[])})
            raw.update({"session_id": sid, "repo": a.get("repo", "."), "repository_root": raw.pop("repo", None), "paths": a.get("paths",[])})
            returned = sum(len(str(raw.get(k, ""))) for k in ("status","diff","staged_diff","untracked","whitespace_errors"))
            raw["returned_chars"] = returned
            raw.setdefault("truncated_sections", [])
            raw.setdefault("status_truncated", "status" in raw["truncated_sections"])
            return raw
        if name == "read_binary_file":
            target = self._join(a.get("repo", "."), a["path"])
            raw = self._remote(sid, "read_binary_file", {"path": target, "max_bytes": a.get("max_bytes",16777216)})
            raw.update({"repo": a.get("repo", "."), "path": a["path"], "uri": self._resource_uri(sid, target)})
            return raw
        if name == "view_image":
            target = self._join(a.get("repo", "."), a["path"])
            raw = self._remote(sid, "view_image", {"path": target, "max_bytes": a.get("max_bytes",16777216)})
            raw.update({"repo": a.get("repo", "."), "path": a["path"], "uri": self._resource_uri(sid, target)})
            return raw
        if name == "write_binary_file":
            target = self._join(a.get("repo", "."), a["path"])
            return self._remote(sid, "write_binary_file", {"path": target, "content_base64": a["content_base64"], "create_parents": a.get("create_parents",True), "overwrite": a.get("overwrite",True)})
        if name == "list_processes":
            return self._remote(sid, "list_processes", {"max_processes": a.get("max_processes",500)})
        if name == "signal_process":
            return self._remote(sid, "signal_process", {"pid": a["pid"], "sig": a.get("signal","TERM")})
        if name == "read_terminal":
            raw = self._remote(sid, "read_terminal", {"terminal_id": sid, "cursor": a.get("cursor"), "max_bytes": a.get("max_bytes",131072), "wait_seconds": a.get("wait_seconds",0), "screen_lines": a.get("screen_lines",200)})
            raw["session_id"] = sid; raw.pop("terminal_id", None); return raw
        if name == "send_terminal_input":
            raw = self._remote(sid, "send_terminal_input", {"terminal_id": sid, "text": a["text"], "press_enter": a.get("press_enter",True)})
            raw["session_id"] = sid; raw.pop("terminal_id", None); return raw
        if name == "send_terminal_key":
            raw = self._remote(sid, "send_terminal_key", {"terminal_id": sid, "key": a["key"]})
            raw["session_id"] = sid; raw.pop("terminal_id", None); return raw
        raise KeyError(f"unknown router tool: {name}")

    @staticmethod
    def _server_meta() -> dict[str, Any]:
        return {"io.modelcontextprotocol/serverInfo": {"name": "host-sandbox-router", "version": __version__}}

    @classmethod
    def _modern_result(cls, payload: dict[str, Any], *, cacheable: bool = False) -> dict[str, Any]:
        result = {**payload, "resultType": "complete", "_meta": cls._server_meta()}
        if cacheable:
            # Match MCP SDK defaults: immediately stale and authorization-private.
            result.update({"cacheScope": "private", "ttlMs": 0})
        return result

    @staticmethod
    def _is_modern(params: dict[str, Any], protocol_version: str | None = None) -> bool:
        if protocol_version == "2026-07-28":
            return True
        meta = params.get("_meta") if isinstance(params, dict) else None
        if not isinstance(meta, dict):
            return False
        version = meta.get("io.modelcontextprotocol/protocolVersion")
        return version == "2026-07-28"

    def handle(self, req: dict[str, Any], protocol_version: str | None = None) -> dict[str, Any] | None:
        rid = req.get("id"); method = req.get("method"); params = req.get("params") or {}
        if rid is None and str(method).startswith("notifications/"): return None
        try:
            if method == "server/discover":
                result = self._modern_result({
                    "supportedVersions": ["2026-07-28"],
                    "capabilities": {"tools": {"listChanged": False}},
                    "instructions": "Multiple computers exposed through Development-Sandbox-style logical sessions. Tools execute directly on host OSes.",
                }, cacheable=True)
            elif method == "initialize":
                requested = str(params.get("protocolVersion") or "2025-11-25")
                negotiated = requested if requested in {"2025-06-18", "2025-11-25"} else "2025-11-25"
                result = {"protocolVersion": negotiated, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "host-sandbox-router", "version": __version__}, "instructions": "Multiple computers exposed through Development-Sandbox-style logical sessions. Tools execute directly on host OSes."}
            elif method == "ping":
                result = self._modern_result({}) if self._is_modern(params, protocol_version) else {}
            elif method == "tools/list":
                payload = {"tools": ROUTER_TOOLS}
                result = self._modern_result(payload, cacheable=True) if self._is_modern(params, protocol_version) else payload
            elif method == "tools/call":
                name = params.get("name"); arguments = dict(params.get("arguments") or {})
                try:
                    payload = self.call(str(name), arguments)
                    if name == "read_binary_file":
                        data = str(payload.pop("content_base64"))
                        uri = str(payload.get("uri"))
                        mime = str(payload.get("mime_type") or "application/octet-stream")
                        body = {
                            "content": [{"type": "resource", "resource": {"uri": uri, "mimeType": mime, "blob": data}}],
                            "structuredContent": payload, "isError": False,
                        }
                    elif name == "view_image":
                        data = str(payload.pop("content_base64"))
                        mime = str(payload.get("mime_type") or "image/png")
                        body = {
                            "content": [{"type": "image", "data": data, "mimeType": mime}],
                            "structuredContent": payload, "isError": False,
                        }
                    else:
                        body = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str, indent=2)}], "structuredContent": payload, "isError": False}
                except Exception as exc:
                    body = {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
                result = self._modern_result(body) if self._is_modern(params, protocol_version) else body
            else:
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}
