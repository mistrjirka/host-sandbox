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
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audit import AuditLog


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _human_mode(mode: int) -> str:
    return stat.filemode(mode)


def _decode_output(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


@dataclass
class Job:
    id: str
    command: str
    cwd: str
    started_at: float
    timeout_seconds: int
    process: subprocess.Popen[bytes]
    log_path: Path
    log_file: Any = field(repr=False)
    timed_out: bool = False
    timeout_timer: threading.Timer | None = field(default=None, repr=False)

    def status(self) -> str:
        rc = self.process.poll()
        if rc is None:
            return "running"
        if self.timed_out:
            return "timed_out"
        return "finished" if rc == 0 else "failed"


class HostTools:
    """Intentionally unrestricted host tools. Paths are not sandboxed."""

    def __init__(self, audit: AuditLog, state_dir: Path, cwd: str | None = None) -> None:
        self.audit = audit
        self.state_dir = state_dir
        self.jobs_dir = state_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.cwd = str(Path(cwd or os.getcwd()).expanduser().resolve())
        self._jobs: dict[str, Job] = {}
        self._jobs_lock = threading.RLock()

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

    def search_text(self, query: str, path: str = ".", max_results: int = 200, fixed_strings: bool = False) -> dict[str, Any]:
        root = self._path(path)
        limit = _clamp(max_results, 1, 2000)
        rg = shutil.which("rg")
        if rg:
            args = [rg, "--line-number", "--column", "--no-heading", "--color", "never", "--hidden", "--glob", "!.git/**"]
            if fixed_strings:
                args.append("--fixed-strings")
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
                proc.kill()
            rc = proc.returncode
            if rc not in {0, 1, -15} and not (rc is None and results):
                raise RuntimeError(stderr.strip() or f"rg failed with exit code {rc}")
            return {"root": str(root), "query": query, "results": results, "truncated": len(results) >= limit, "engine": "rg"}

        results = []
        paths = [root] if root.is_file() else (Path(dp) / n for dp, _, files in os.walk(root) for n in files)
        needle = query if fixed_strings else query
        for file_path in paths:
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

    def _kill_job(self, job: Job, timed_out: bool = False) -> None:
        if job.process.poll() is not None:
            return
        job.timed_out = timed_out
        try:
            if os.name == "posix":
                os.killpg(job.process.pid, signal.SIGTERM)
            else:
                job.process.terminate()
        except ProcessLookupError:
            return
        try:
            job.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(job.process.pid, signal.SIGKILL)
                else:
                    job.process.kill()
            except ProcessLookupError:
                pass

    def _job_dict(self, job: Job, output_bytes: int = 65536) -> dict[str, Any]:
        rc = job.process.poll()
        try:
            size = job.log_path.stat().st_size
            with job.log_path.open("rb") as f:
                if size > output_bytes:
                    f.seek(size - output_bytes)
                output = _decode_output(f.read())
        except OSError:
            size = 0
            output = ""
        return {
            "id": job.id,
            "command": job.command,
            "cwd": job.cwd,
            "pid": job.process.pid,
            "status": job.status(),
            "exit_code": rc,
            "started_at": job.started_at,
            "duration_seconds": max(0.0, time.time() - job.started_at),
            "timeout_seconds": job.timeout_seconds,
            "output_size": size,
            "output_tail": output,
        }

    def exec_command(
        self,
        command: str,
        cwd: str | None = None,
        timeout_seconds: int = 3600,
        wait_seconds: int = 8,
        max_output_bytes: int = 131072,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        run_cwd = str(self._path(cwd or self.cwd))
        timeout_seconds = _clamp(timeout_seconds, 1, 604800)
        wait_seconds = _clamp(wait_seconds, 0, 20)
        max_output_bytes = _clamp(max_output_bytes, 1000, 2_097_152)
        job_id = "j_" + uuid.uuid4().hex[:16]
        log_path = self.jobs_dir / f"{job_id}.log"
        log_file = log_path.open("wb")
        child_env = os.environ.copy()
        if env:
            child_env.update({str(k): str(v) for k, v in env.items()})
        if os.name == "posix":
            shell_path = child_env.get("SHELL") or "/bin/bash"
            if not (os.path.isabs(shell_path) and os.access(shell_path, os.X_OK)):
                shell_path = "/bin/bash"
            argv: Any = [shell_path, "-c", command]
        else:
            argv = command
        proc = subprocess.Popen(
            argv,
            cwd=run_cwd,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name == "posix"),
            shell=(os.name != "posix"),
        )
        job = Job(job_id, command, run_cwd, time.time(), timeout_seconds, proc, log_path, log_file)
        with self._jobs_lock:
            self._jobs[job_id] = job
        timer = threading.Timer(timeout_seconds, self._kill_job, args=(job, True))
        timer.daemon = True
        job.timeout_timer = timer
        timer.start()

        if wait_seconds > 0:
            try:
                proc.wait(timeout=wait_seconds)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is not None:
            timer.cancel()
            log_file.flush()
            log_file.close()
        return self._job_dict(job, max_output_bytes)

    def exec_commands(self, commands: list[dict[str, Any]], concurrency: int = 8) -> dict[str, Any]:
        if not commands:
            return {"results": []}
        if len(commands) > 32:
            raise ValueError("at most 32 commands are allowed")
        concurrency = _clamp(concurrency, 1, 16)

        def run(item: dict[str, Any]) -> dict[str, Any]:
            return self.exec_command(**item)

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(run, item) for item in commands]
            return {"results": [future.result() for future in futures]}

    def list_jobs(self, limit: int = 100) -> dict[str, Any]:
        with self._jobs_lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)[: _clamp(limit, 1, 1000)]
        return {"jobs": [self._job_dict(job, 8192) for job in jobs]}

    def read_job(self, job_id: str, offset: int = 0, max_bytes: int = 131072) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if not job:
            raise KeyError(f"unknown job: {job_id}")
        offset = max(0, int(offset))
        max_bytes = _clamp(max_bytes, 1, 2_097_152)
        try:
            job.log_file.flush()
        except (ValueError, OSError):
            pass
        size = job.log_path.stat().st_size
        with job.log_path.open("rb") as f:
            f.seek(offset)
            data = f.read(max_bytes)
        info = self._job_dict(job, 0)
        info.update({"offset": offset, "next_offset": offset + len(data), "eof": offset + len(data) >= size, "output": _decode_output(data)})
        return info

    def signal_job(self, job_id: str, sig: str = "TERM") -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if not job:
            raise KeyError(f"unknown job: {job_id}")
        if job.process.poll() is not None:
            return self._job_dict(job)
        signum = getattr(signal, f"SIG{sig.upper()}", None)
        if signum is None:
            raise ValueError("unsupported signal")
        if os.name == "posix":
            os.killpg(job.process.pid, signum)
        else:
            job.process.send_signal(signum)
        return self._job_dict(job)

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
        os.kill(int(pid), signum)
        return {"pid": int(pid), "signal": sig.upper(), "sent": True}

    def close(self) -> None:
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.process.poll() is None:
                self._kill_job(job)
            try:
                job.log_file.close()
            except (OSError, ValueError):
                pass
