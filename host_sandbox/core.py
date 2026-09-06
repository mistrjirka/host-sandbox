from __future__ import annotations

import base64
import concurrent.futures
import fnmatch
import hashlib
import json
import os
import platform
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .parity import ParityHostToolsMixin


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _human_mode(mode: int) -> str:
    return stat.filemode(mode)


def _decode_output(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


JOB_STATE_ENV = "HOST_SANDBOX_STATE_ID"
JOB_ID_ENV = "HOST_SANDBOX_JOB_ID"
RESERVED_JOB_ENV = frozenset({JOB_STATE_ENV, JOB_ID_ENV})
TERMINAL_JOB_STATES = frozenset({"finished", "failed", "timed_out", "cancelled", "interrupted"})
NONTERMINAL_JOB_STATES = frozenset({"queued", "waiting_for_lock", "running"})
DEFAULT_RESOURCE_LOCK_WAIT_SECONDS = 30


@dataclass
class Job:
    id: str
    command: str
    cwd: str
    started_at: float
    timeout_seconds: int
    process: subprocess.Popen[bytes] | None
    stdout_path: Path
    stderr_path: Path
    state_path: Path
    spec_path: Path
    cancel_path: Path
    resource_locks: list[str] = field(default_factory=list)
    resource_lock_wait_seconds: int = DEFAULT_RESOURCE_LOCK_WAIT_SECONDS
    stdout_file: Any | None = field(default=None, repr=False)
    stderr_file: Any | None = field(default=None, repr=False)

    def state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text())
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def status(self) -> str:
        state = self.state()
        status = str(state.get("status") or "")
        if status:
            return status
        if self.process is not None:
            rc = self.process.poll()
            if rc is None:
                return "queued"
            return "finished" if rc == 0 else "failed"
        return "interrupted"


class HostTools(ParityHostToolsMixin):
    """Intentionally unrestricted host tools. Paths are not sandboxed."""

    def __init__(self, audit: AuditLog, state_dir: Path, cwd: str | None = None) -> None:
        self.audit = audit
        self.state_dir = state_dir
        self.jobs_dir = state_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.resource_locks_dir = state_dir / "resource-locks"
        self.resource_locks_dir.mkdir(parents=True, exist_ok=True)
        self.cwd = str(Path(cwd or os.getcwd()).expanduser().resolve())
        self._state_id = hashlib.sha256(str(state_dir.expanduser().resolve()).encode()).hexdigest()[:24]
        self._jobs: dict[str, Job] = {}
        self._jobs_lock = threading.RLock()
        self._load_persisted_jobs()

    def _path(self, raw: str | None) -> Path:
        if raw is None or raw == "":
            return Path(self.cwd)
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = Path(self.cwd) / p
        return p.resolve(strict=False)

    def system_info(self) -> dict[str, Any]:
        return {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "pid": os.getpid(),
            "uid": os.getuid() if hasattr(os, "getuid") else None,
            "euid": os.geteuid() if hasattr(os, "geteuid") else None,
            "cwd": self.cwd,
            "home": str(Path.home()),
            "user": os.environ.get("USER") or os.environ.get("USERNAME"),
            "host_mode": True,
            "sandboxed": False,
        }

    def path_info(self, path: str) -> dict[str, Any]:
        p = self._path(path)
        st = p.lstat()
        kind = "directory" if p.is_dir() else "file" if p.is_file() else "symlink" if p.is_symlink() else "other"
        result: dict[str, Any] = {
            "path": str(p),
            "kind": kind,
            "size": st.st_size,
            "mode": oct(stat.S_IMODE(st.st_mode)),
            "mode_text": _human_mode(st.st_mode),
            "mtime": st.st_mtime,
            "ctime": st.st_ctime,
            "uid": st.st_uid if hasattr(st, "st_uid") else None,
            "gid": st.st_gid if hasattr(st, "st_gid") else None,
        }
        if p.is_symlink():
            result["symlink_target"] = os.readlink(p)
        return result

    def list_dir(self, path: str = ".", max_entries: int = 500) -> dict[str, Any]:
        p = self._path(path)
        limit = _clamp(max_entries, 1, 5000)
        entries = []
        with os.scandir(p) as it:
            for entry in it:
                if len(entries) >= limit:
                    break
                try:
                    st = entry.stat(follow_symlinks=False)
                    entries.append({
                        "name": entry.name,
                        "path": str(Path(entry.path).resolve(strict=False)),
                        "kind": "directory" if entry.is_dir(follow_symlinks=False) else "file" if entry.is_file(follow_symlinks=False) else "symlink" if entry.is_symlink() else "other",
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "mode": oct(stat.S_IMODE(st.st_mode)),
                    })
                except OSError as exc:
                    entries.append({"name": entry.name, "path": entry.path, "error": str(exc)})
        entries.sort(key=lambda x: (x.get("kind") != "directory", x.get("name", "").lower()))
        return {"path": str(p), "entries": entries, "truncated": len(entries) >= limit}

    def read_file(self, path: str, offset: int = 0, max_bytes: int = 262144) -> dict[str, Any]:
        p = self._path(path)
        max_bytes = _clamp(max_bytes, 1, 1_048_576)
        offset = max(0, int(offset))
        size = p.stat().st_size
        with p.open("rb") as f:
            f.seek(offset)
            data = f.read(max_bytes)
        return {
            "path": str(p),
            "offset": offset,
            "next_offset": offset + len(data),
            "size": size,
            "eof": offset + len(data) >= size,
            "text": _decode_output(data),
        }

    def read_file_chunk(self, path: str, offset: int = 0, length: int = 524288) -> dict[str, Any]:
        p = self._path(path)
        length = _clamp(length, 1, 1_048_576)
        offset = max(0, int(offset))
        size = p.stat().st_size
        with p.open("rb") as f:
            f.seek(offset)
            data = f.read(length)
        return {
            "path": str(p),
            "offset": offset,
            "next_offset": offset + len(data),
            "size": size,
            "eof": offset + len(data) >= size,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "data_base64": base64.b64encode(data).decode("ascii"),
        }

    def write_file(self, path: str, content: str, mode: str = "overwrite", create_parents: bool = True) -> dict[str, Any]:
        p = self._path(path)
        if create_parents:
            p.parent.mkdir(parents=True, exist_ok=True)
        if mode not in {"overwrite", "append", "exclusive"}:
            raise ValueError("mode must be overwrite, append, or exclusive")
        open_mode = {"overwrite": "w", "append": "a", "exclusive": "x"}[mode]
        with p.open(open_mode, encoding="utf-8") as f:
            written = f.write(content)
        return {"path": str(p), "characters": written, "bytes": p.stat().st_size, "mode": mode}

    def replace_text(self, path: str, old: str, new: str, count: int = 0, expected_matches: int | None = None) -> dict[str, Any]:
        p = self._path(path)
        text = p.read_text(encoding="utf-8")
        matches = text.count(old)
        if expected_matches is not None and matches != int(expected_matches):
            raise ValueError(f"expected {expected_matches} matches, found {matches}")
        if matches == 0:
            raise ValueError("old text was not found")
        updated = text.replace(old, new, int(count) if int(count) > 0 else -1)
        p.write_text(updated, encoding="utf-8")
        replaced = min(matches, int(count)) if int(count) > 0 else matches
        return {"path": str(p), "matches": matches, "replaced": replaced, "bytes": p.stat().st_size}

    def write_file_chunk(
        self,
        path: str,
        data_base64: str,
        offset: int = 0,
        create_parents: bool = True,
        truncate_after: bool = False,
        final_sha256: bool = False,
    ) -> dict[str, Any]:
        p = self._path(path)
        if create_parents:
            p.parent.mkdir(parents=True, exist_ok=True)
        data = base64.b64decode(data_base64, validate=True)
        if len(data) > 1_048_576:
            raise ValueError("decoded chunk exceeds 1 MiB")
        offset = max(0, int(offset))
        file_mode = "r+b" if p.exists() else "w+b"
        with p.open(file_mode) as f:
            f.seek(offset)
            f.write(data)
            if truncate_after:
                f.truncate(offset + len(data))
        result: dict[str, Any] = {
            "path": str(p),
            "offset": offset,
            "next_offset": offset + len(data),
            "bytes_written": len(data),
            "size": p.stat().st_size,
            "chunk_sha256": hashlib.sha256(data).hexdigest(),
        }
        if final_sha256:
            h = hashlib.sha256()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            result["file_sha256"] = h.hexdigest()
        return result

    def hash_file(self, path: str, algorithm: str = "sha256") -> dict[str, Any]:
        p = self._path(path)
        try:
            h = hashlib.new(algorithm)
        except ValueError as exc:
            raise ValueError(f"unsupported hash algorithm: {algorithm}") from exc
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return {"path": str(p), "algorithm": algorithm, "digest": h.hexdigest(), "size": p.stat().st_size}

    def file_op(
        self,
        action: str,
        path: str,
        destination: str | None = None,
        recursive: bool = False,
        mode: str | None = None,
    ) -> dict[str, Any]:
        p = self._path(path)
        if action == "mkdir":
            p.mkdir(parents=recursive, exist_ok=True)
        elif action == "touch":
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch(exist_ok=True)
        elif action == "remove":
            if p.is_dir() and not p.is_symlink():
                if recursive:
                    shutil.rmtree(p)
                else:
                    p.rmdir()
            else:
                p.unlink()
        elif action in {"copy", "move"}:
            if not destination:
                raise ValueError("destination is required for copy/move")
            dest = self._path(destination)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if action == "move":
                shutil.move(str(p), str(dest))
            elif p.is_dir():
                if not recursive:
                    raise ValueError("recursive=true is required to copy directories")
                shutil.copytree(p, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(p, dest)
            return {"action": action, "path": str(p), "destination": str(dest)}
        elif action == "chmod":
            if mode is None:
                raise ValueError("mode is required for chmod, e.g. 755")
            os.chmod(p, int(mode, 8))
        else:
            raise ValueError("action must be mkdir, touch, remove, copy, move, or chmod")
        return {"action": action, "path": str(p), "exists": p.exists()}

    def find_paths(self, pattern: str, path: str = ".", max_results: int = 500, max_depth: int = 20) -> dict[str, Any]:
        root = self._path(path)
        limit = _clamp(max_results, 1, 5000)
        max_depth = _clamp(max_depth, 0, 100)
        results: list[str] = []
        root_depth = len(root.parts)
        if root.is_file():
            candidates = [root]
        else:
            candidates = []
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                current = Path(dirpath)
                depth = len(current.parts) - root_depth
                if depth >= max_depth:
                    dirnames[:] = []
                for name in dirnames + filenames:
                    candidates.append(current / name)
                    if len(candidates) > limit * 20:
                        break
                if len(candidates) > limit * 20:
                    break
        for candidate in candidates:
            try:
                rel = str(candidate.relative_to(root)) if candidate != root else candidate.name
            except ValueError:
                rel = str(candidate)
            if fnmatch.fnmatch(candidate.name, pattern) or fnmatch.fnmatch(rel, pattern):
                results.append(str(candidate))
                if len(results) >= limit:
                    break
        return {"root": str(root), "pattern": pattern, "results": results, "truncated": len(results) >= limit}

    def search_text(self, query: str, path: str = ".", max_results: int = 200, fixed_strings: bool = False, glob: list[str] | None = None) -> dict[str, Any]:
        root = self._path(path)
        limit = _clamp(max_results, 1, 2000)
        rg = shutil.which("rg")
        if rg:
            args = [rg, "--line-number", "--column", "--no-heading", "--color", "never", "--hidden", "--glob", "!.git/**"]
            if fixed_strings:
                args.append("--fixed-strings")
            for pattern in glob or []:
                args.extend(["--glob", str(pattern)])
            args.extend([query, str(root)])
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            results: list[str] = []
            assert proc.stdout is not None
            for raw in proc.stdout:
                results.append(_decode_output(raw).rstrip("\n"))
                if len(results) >= limit:
                    proc.terminate()
                    break
            stderr = _decode_output(proc.stderr.read()) if proc.stderr else ""
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait(timeout=2)
            finally:
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()
            rc = proc.returncode
            if rc not in {0, 1, -15} and not (rc is None and results):
                raise RuntimeError(stderr.strip() or f"rg failed with exit code {rc}")
            return {"root": str(root), "query": query, "results": results, "truncated": len(results) >= limit, "engine": "rg"}

        results = []
        paths = [root] if root.is_file() else (Path(dp) / n for dp, _, files in os.walk(root) for n in files)
        needle = query if fixed_strings else query
        for file_path in paths:
            if glob:
                try:
                    rel = str(file_path.relative_to(root)) if root.is_dir() else file_path.name
                except ValueError:
                    rel = str(file_path)
                if not any(fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(file_path.name, pattern) for pattern in glob):
                    continue
            try:
                with file_path.open("r", encoding="utf-8", errors="ignore") as f:
                    for line_no, line in enumerate(f, 1):
                        if needle in line:
                            results.append(f"{file_path}:{line_no}:{line.rstrip()}")
                            if len(results) >= limit:
                                return {"root": str(root), "query": query, "results": results, "truncated": True, "engine": "python"}
            except (OSError, UnicodeError):
                continue
        return {"root": str(root), "query": query, "results": results, "truncated": False, "engine": "python"}

    @staticmethod
    def _write_json_private(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
            os.chmod(tmp, 0o600)
            tmp.replace(path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _job_paths(self, job_id: str) -> tuple[Path, Path, Path, Path, Path]:
        return (
            self.jobs_dir / f"{job_id}.stdout",
            self.jobs_dir / f"{job_id}.stderr",
            self.jobs_dir / f"{job_id}.state.json",
            self.jobs_dir / f"{job_id}.spec.json",
            self.jobs_dir / f"{job_id}.cancel",
        )

    def _job_process_pids(self, job: Job) -> list[int]:
        if os.name != "posix" or not Path("/proc").is_dir():
            if job.process is not None and job.process.poll() is None:
                return [job.process.pid]
            return []
        wanted = {
            f"{JOB_STATE_ENV}={self._state_id}".encode(),
            f"{JOB_ID_ENV}={job.id}".encode(),
        }
        uid = os.getuid()
        found: list[int] = []
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                if proc.stat().st_uid != uid:
                    continue
                process_env = set((proc / "environ").read_bytes().split(b"\0"))
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                continue
            if wanted.issubset(process_env):
                found.append(int(proc.name))
        return sorted(found)

    def _signal_job_processes(self, job: Job, signum: int) -> list[int]:
        signalled: list[int] = []
        for pid in self._job_process_pids(job):
            try:
                os.kill(pid, signum)
                signalled.append(pid)
            except (ProcessLookupError, PermissionError):
                continue
        return signalled

    def _update_job_state(self, job: Job, **updates: Any) -> dict[str, Any]:
        state = job.state()
        state.update(updates)
        self._write_json_private(job.state_path, state)
        return state

    def _load_persisted_jobs(self) -> None:
        for spec_path in sorted(self.jobs_dir.glob("j_*.spec.json")):
            try:
                spec = json.loads(spec_path.read_text())
                job_id = str(spec["id"])
                stdout_path, stderr_path, state_path, _, cancel_path = self._job_paths(job_id)
                job = Job(
                    id=job_id,
                    command=str(spec.get("command") or ""),
                    cwd=str(spec.get("cwd") or self.cwd),
                    started_at=float(spec.get("created_at") or time.time()),
                    timeout_seconds=int(spec.get("timeout_seconds") or 3600),
                    process=None,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    state_path=state_path,
                    spec_path=spec_path,
                    cancel_path=cancel_path,
                    resource_locks=list(spec.get("resource_locks") or []),
                    resource_lock_wait_seconds=int(
                        spec.get("resource_lock_wait_seconds", DEFAULT_RESOURCE_LOCK_WAIT_SECONDS)
                    ),
                )
                if job.status() in NONTERMINAL_JOB_STATES and not self._job_process_pids(job):
                    self._update_job_state(
                        job,
                        status="interrupted",
                        exit_code=125,
                        finished_at=time.time(),
                        interrupted_reason="controller_recovered_without_runner",
                    )
                self._jobs[job_id] = job
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue

    @staticmethod
    def _read_stream(path: Path, offset: int, limit: int) -> tuple[str, int, int, bool]:
        try:
            size = path.stat().st_size
        except OSError:
            return "", offset, 0, False
        offset = max(0, min(int(offset), size))
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read(max(0, limit))
        next_offset = offset + len(data)
        return _decode_output(data), next_offset, size, next_offset < size

    @staticmethod
    def _close_job_streams(job: Job) -> None:
        if job.process is not None:
            job.process.poll()
            if job.process.returncode is None and job.status() in TERMINAL_JOB_STATES:
                try:
                    job.process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (job.stdout_file, job.stderr_file):
            if stream is None:
                continue
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        job.stdout_file = None
        job.stderr_file = None

    def _job_dict(self, job: Job, output_bytes: int = 65536) -> dict[str, Any]:
        state = job.state()
        status = str(state.get("status") or job.status())
        if status in NONTERMINAL_JOB_STATES and job.process is not None:
            runner_rc = job.process.poll()
            if runner_rc is not None and not self._job_process_pids(job):
                state = self._update_job_state(
                    job,
                    status="failed",
                    exit_code=int(runner_rc),
                    finished_at=time.time(),
                    interrupted_reason="runner_exited_without_terminal_state",
                )
                status = "failed"
        if status in TERMINAL_JOB_STATES:
            self._close_job_streams(job)
        exit_code = state.get("exit_code")
        if exit_code is None and job.process is not None and job.process.poll() is not None and status not in NONTERMINAL_JOB_STATES:
            exit_code = job.process.returncode
        budget = max(0, int(output_bytes))
        stdout, stdout_next, stdout_size, stdout_more = self._read_stream(job.stdout_path, 0, budget)
        used = len(stdout.encode("utf-8", errors="replace"))
        stderr_budget = max(0, budget - used)
        stderr, stderr_next, stderr_size, stderr_more = self._read_stream(job.stderr_path, 0, stderr_budget)
        returned = len(stdout.encode("utf-8", errors="replace")) + len(stderr.encode("utf-8", errors="replace"))
        finished_at = state.get("finished_at")
        duration = max(0.0, float(finished_at or time.time()) - job.started_at)
        return {
            "id": job.id,
            "command": job.command,
            "cwd": job.cwd,
            "pid": state.get("child_pid") or state.get("runner_pid") or (job.process.pid if job.process else None),
            "runner_pid": state.get("runner_pid"),
            "status": status,
            "exit_code": exit_code,
            "started_at": state.get("started_at") or job.started_at,
            "running_at": state.get("running_at"),
            "finished_at": finished_at,
            "duration_seconds": duration,
            "duration_ms": round(duration * 1000, 3),
            "timeout_seconds": job.timeout_seconds,
            "timeout_stage": state.get("timeout_stage"),
            "interrupted_reason": state.get("interrupted_reason"),
            "resource_locks": list(job.resource_locks),
            "resource_lock_wait_seconds": job.resource_lock_wait_seconds,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_offset": 0,
            "stdout_next_offset": stdout_next,
            "stdout_more": stdout_more,
            "stdout_size": stdout_size,
            "stderr_offset": 0,
            "stderr_next_offset": stderr_next,
            "stderr_more": stderr_more,
            "stderr_size": stderr_size,
            "max_output_bytes_total": budget,
            "returned_output_bytes": returned,
            "output_tail": stdout + stderr,
            "output_size": stdout_size + stderr_size,
        }

    @staticmethod
    def _normalize_resource_locks(names: list[str] | None) -> list[str]:
        normalized = sorted({str(name) for name in (names or []) if str(name)})
        if len(normalized) > 16:
            raise ValueError("at most 16 resource locks are allowed")
        for name in normalized:
            if len(name) > 100 or any(not (ch.isalnum() or ch in "_.:-") for ch in name):
                raise ValueError(f"invalid resource lock name: {name!r}")
        return normalized

    def exec_command(
        self,
        command: str,
        cwd: str | None = None,
        timeout_seconds: int = 3600,
        wait_seconds: int = 8,
        max_output_bytes: int = 131072,
        env: dict[str, str] | None = None,
        resource_locks: list[str] | None = None,
        resource_lock_wait_seconds: int | None = None,
    ) -> dict[str, Any]:
        run_cwd = str(self._path(cwd or self.cwd))
        timeout_seconds = _clamp(timeout_seconds, 1, 604800)
        wait_seconds = _clamp(wait_seconds, 0, 20)
        max_output_bytes = _clamp(max_output_bytes, 1000, 2_097_152)
        lock_wait = DEFAULT_RESOURCE_LOCK_WAIT_SECONDS if resource_lock_wait_seconds is None else _clamp(resource_lock_wait_seconds, 0, 604800)
        locks = self._normalize_resource_locks(resource_locks)
        job_id = "j_" + uuid.uuid4().hex[:16]
        stdout_path, stderr_path, state_path, spec_path, cancel_path = self._job_paths(job_id)
        child_env = os.environ.copy()
        if env:
            for key, value in env.items():
                key = str(key)
                if key in RESERVED_JOB_ENV:
                    raise ValueError(f"reserved environment variable: {key}")
                child_env[key] = str(value)
        child_env[JOB_STATE_ENV] = self._state_id
        child_env[JOB_ID_ENV] = job_id
        if os.name == "posix":
            shell_path = child_env.get("SHELL") or "/bin/bash"
            if not (os.path.isabs(shell_path) and os.access(shell_path, os.X_OK)):
                shell_path = "/bin/bash"
        else:
            shell_path = child_env.get("COMSPEC") or "cmd.exe"
        created_at = time.time()
        spec = {
            "id": job_id,
            "state_id": self._state_id,
            "command": command,
            "cwd": run_cwd,
            "shell": shell_path,
            "created_at": created_at,
            "timeout_seconds": timeout_seconds,
            "resource_locks": locks,
            "resource_lock_wait_seconds": lock_wait,
            "state_path": str(state_path),
            "cancel_path": str(cancel_path),
            "lock_dir": str(self.resource_locks_dir),
        }
        self._write_json_private(spec_path, spec)
        self._write_json_private(
            state_path,
            {
                "id": job_id,
                "status": "queued",
                "started_at": created_at,
                "exit_code": None,
                "timeout_stage": None,
                "interrupted_reason": None,
            },
        )
        try:
            cancel_path.unlink()
        except FileNotFoundError:
            pass
        stdout_file = stdout_path.open("wb")
        stderr_file = stderr_path.open("wb")
        runner_path = Path(__file__).with_name("job_runner.py")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(runner_path), str(spec_path)],
                cwd=run_cwd,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=(os.name == "posix"),
            )
        except Exception:
            stdout_file.close()
            stderr_file.close()
            for partial in (stdout_path, stderr_path, state_path, spec_path, cancel_path):
                try:
                    partial.unlink()
                except FileNotFoundError:
                    pass
            raise
        job = Job(
            job_id,
            command,
            run_cwd,
            created_at,
            timeout_seconds,
            proc,
            stdout_path,
            stderr_path,
            state_path,
            spec_path,
            cancel_path,
            locks,
            lock_wait,
            stdout_file,
            stderr_file,
        )
        with self._jobs_lock:
            self._jobs[job_id] = job
        if wait_seconds > 0:
            try:
                proc.wait(timeout=wait_seconds)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is not None:
            self._close_job_streams(job)
        return self._job_dict(job, max_output_bytes)

    def exec_commands(self, commands: list[dict[str, Any]], concurrency: int = 8) -> dict[str, Any]:
        if not commands:
            return {"results": []}
        if len(commands) > 32:
            raise ValueError("at most 32 commands are allowed")
        concurrency = _clamp(concurrency, 1, 16)
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(self.exec_command, **item) for item in commands]
            return {"results": [future.result() for future in futures]}

    def list_jobs(self, limit: int = 100) -> dict[str, Any]:
        with self._jobs_lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)[: _clamp(limit, 1, 10000)]
        return {"jobs": [self._job_dict(job, 8192) for job in jobs]}

    def read_job(
        self, job_id: str, stdout_offset: int = 0, stderr_offset: int = 0, max_bytes: int = 131072
    ) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if not job:
            raise KeyError(f"unknown job: {job_id}")
        for stream in (job.stdout_file, job.stderr_file):
            if stream is not None:
                try:
                    stream.flush()
                except (OSError, ValueError):
                    pass
        budget = _clamp(max_bytes, 1000, 2_097_152)
        stdout, stdout_next, stdout_size, stdout_more = self._read_stream(job.stdout_path, stdout_offset, budget)
        used = len(stdout.encode("utf-8", errors="replace"))
        stderr, stderr_next, stderr_size, stderr_more = self._read_stream(job.stderr_path, stderr_offset, max(0, budget-used))
        info = self._job_dict(job, 0)
        info.update({
            "stdout": stdout, "stderr": stderr,
            "stdout_offset": max(0, int(stdout_offset)), "stdout_next_offset": stdout_next, "stdout_more": stdout_more, "stdout_size": stdout_size,
            "stderr_offset": max(0, int(stderr_offset)), "stderr_next_offset": stderr_next, "stderr_more": stderr_more, "stderr_size": stderr_size,
            "max_output_bytes_total": budget,
            "returned_output_bytes": len(stdout.encode("utf-8", errors="replace")) + len(stderr.encode("utf-8", errors="replace")),
            "output": stdout + stderr,
        })
        return info

    def delete_job(self, job_id: str) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(f"unknown job: {job_id}")
            if job.status() not in TERMINAL_JOB_STATES:
                raise RuntimeError("cannot delete a running job")
            self._jobs.pop(job_id, None)
        self._close_job_streams(job)
        deleted = []
        for path in (job.stdout_path, job.stderr_path, job.state_path, job.spec_path, job.cancel_path):
            try:
                path.unlink(); deleted.append(str(path))
            except FileNotFoundError:
                pass
        return {"job_id": job_id, "deleted": True, "files": deleted}

    def cleanup_jobs(
        self, older_than_seconds: int = 604800, keep_recent: int = 20, max_delete: int = 10000, dry_run: bool = True
    ) -> dict[str, Any]:
        age = _clamp(older_than_seconds, 0, 31_536_000)
        keep = _clamp(keep_recent, 0, 10000)
        cap = _clamp(max_delete, 1, 100000)
        now = time.time()
        with self._jobs_lock:
            finished = [j for j in self._jobs.values() if j.status() in TERMINAL_JOB_STATES]
            finished.sort(key=lambda j: j.started_at, reverse=True)
            protected = {j.id for j in finished[:keep]}
            candidates = [j for j in finished if j.id not in protected and now-j.started_at >= age][:cap]
        rows = [{"job_id": j.id, "started_at": j.started_at, "age_seconds": round(now-j.started_at,3)} for j in candidates]
        if not dry_run:
            for j in candidates:
                self.delete_job(j.id)
        return {"dry_run": bool(dry_run), "candidate_count": len(rows), "deleted_count": 0 if dry_run else len(rows), "jobs": rows}

    def signal_job(self, job_id: str, sig: str = "TERM", force_after_seconds: int = 5) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if not job:
            raise KeyError(f"unknown job: {job_id}")
        if job.status() in TERMINAL_JOB_STATES:
            return self._job_dict(job)
        signum = getattr(signal, f"SIG{sig.upper()}", None)
        if signum is None:
            raise ValueError("unsupported signal")
        cancelling = sig.upper() not in {"CONT", "STOP"}
        if cancelling:
            job.cancel_path.write_text(sig.upper() + "\n")
            os.chmod(job.cancel_path, 0o600)
        signalled = self._signal_job_processes(job, int(signum))
        if not signalled and job.process is not None and job.process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(job.process.pid, int(signum))
                else:
                    job.process.send_signal(int(signum))
                signalled.append(job.process.pid)
            except ProcessLookupError:
                pass
        force = _clamp(force_after_seconds, 0, 60)
        if cancelling and force and sig.upper() != "KILL":
            deadline = time.monotonic() + force
            while time.monotonic() < deadline:
                if not self._job_process_pids(job) or job.status() in TERMINAL_JOB_STATES:
                    break
                time.sleep(0.05)
            if self._job_process_pids(job):
                self._signal_job_processes(job, int(signal.SIGKILL))
        if cancelling:
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and self._job_process_pids(job):
                time.sleep(0.025)
            if not self._job_process_pids(job) and job.status() not in TERMINAL_JOB_STATES:
                self._update_job_state(job, status="cancelled", exit_code=137 if sig.upper() == "KILL" else 143, finished_at=time.time())
        return {**self._job_dict(job), "signal_sent": bool(signalled), "pids_signalled": signalled}

    def list_processes(self, max_processes: int = 500) -> dict[str, Any]:
        limit = _clamp(max_processes, 1, 10000)
        if os.name != "posix":
            raise RuntimeError("list_processes currently requires a POSIX host")
        proc = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,user=,stat=,etimes=,pcpu=,pmem=,args="],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        lines = _decode_output(proc.stdout).splitlines()[:limit]
        processes = []
        for line in lines:
            parts = line.strip().split(None, 7)
            if len(parts) < 8:
                continue
            pid, ppid, user, state, elapsed, cpu, mem, command = parts
            processes.append({
                "pid": int(pid), "ppid": int(ppid), "user": user, "state": state,
                "elapsed_seconds": int(elapsed), "cpu_percent": float(cpu), "memory_percent": float(mem), "command": command,
            })
        return {"processes": processes, "truncated": len(lines) >= limit}

    def signal_process(self, pid: int, sig: str = "TERM") -> dict[str, Any]:
        signum = getattr(signal, f"SIG{sig.upper()}", None)
        if signum is None:
            raise ValueError("unsupported signal")
        target = int(pid)
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            state = job.state()
            main_pids = {state.get("runner_pid"), state.get("child_pid")}
            if job.process is not None:
                main_pids.add(job.process.pid)
            if target in main_pids and job.status() not in TERMINAL_JOB_STATES:
                result = self.signal_job(job.id, sig, 0)
                return {
                    "pid": target,
                    "signal": sig.upper(),
                    "sent": bool(result.get("signal_sent", True)),
                    "associated_job_id": job.id,
                }
        os.kill(target, signum)
        return {"pid": target, "signal": sig.upper(), "sent": True, "associated_job_id": None}

    def close(self) -> None:
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.status() not in TERMINAL_JOB_STATES:
                try:
                    self.signal_job(job.id, "TERM", 2)
                except (OSError, RuntimeError):
                    self._signal_job_processes(job, int(signal.SIGKILL))
            self._close_job_streams(job)
