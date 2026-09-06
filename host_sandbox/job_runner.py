from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - resource locks are POSIX-only today
    fcntl = None  # type: ignore[assignment]

STATE_ENV = "HOST_SANDBOX_STATE_ID"
JOB_ENV = "HOST_SANDBOX_JOB_ID"
_RECEIVED_SIGNAL: int | None = None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _write_state(path: Path, **updates: Any) -> None:
    state = _read_json(path)
    state.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _marker_pids(state_id: str, job_id: str) -> list[int]:
    if os.name != "posix" or not Path("/proc").is_dir():
        return []
    wanted = {f"{STATE_ENV}={state_id}".encode(), f"{JOB_ENV}={job_id}".encode()}
    uid = os.getuid()
    result: list[int] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        pid = int(proc.name)
        if pid == os.getpid():
            continue
        try:
            if proc.stat().st_uid != uid:
                continue
            env = set((proc / "environ").read_bytes().split(b"\0"))
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        if wanted.issubset(env):
            result.append(pid)
    return sorted(result)


def _signal_marked(state_id: str, job_id: str, signum: int) -> list[int]:
    signalled: list[int] = []
    for pid in _marker_pids(state_id, job_id):
        try:
            os.kill(pid, signum)
            signalled.append(pid)
        except (ProcessLookupError, PermissionError):
            continue
    return signalled


def _cleanup_marked(state_id: str, job_id: str, first_signal: int = signal.SIGTERM) -> None:
    _signal_marked(state_id, job_id, first_signal)
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline:
        if not _marker_pids(state_id, job_id):
            return
        time.sleep(0.025)
    _signal_marked(state_id, job_id, signal.SIGKILL)
    time.sleep(0.05)
    _signal_marked(state_id, job_id, signal.SIGKILL)


def _on_signal(signum: int, _frame: object) -> None:
    global _RECEIVED_SIGNAL
    _RECEIVED_SIGNAL = signum


def _cancelled(cancel_path: Path) -> bool:
    return cancel_path.exists()


def _terminal_from_signal(state_path: Path, cancel_path: Path, signum: int) -> int:
    now = time.time()
    status = "cancelled" if _cancelled(cancel_path) else "interrupted"
    update: dict[str, Any] = {
        "status": status,
        "exit_code": 128 + int(signum),
        "finished_at": now,
    }
    if status == "interrupted":
        update["interrupted_reason"] = f"signal_{signal.Signals(signum).name.lower()}"
    _write_state(state_path, **update)
    return 128 + int(signum)


def _acquire_locks(
    names: list[str], lock_dir: Path, wait_seconds: int, state_path: Path, cancel_path: Path
) -> tuple[list[Any], bool]:
    if not names:
        return [], True
    if fcntl is None:
        _write_state(
            state_path,
            status="failed",
            exit_code=127,
            finished_at=time.time(),
            interrupted_reason="resource_locks_require_posix_fcntl",
        )
        return [], False

    lock_dir.mkdir(parents=True, exist_ok=True)
    handles: list[Any] = []
    deadline = time.monotonic() + max(0, int(wait_seconds))
    _write_state(state_path, status="waiting_for_lock")

    for name in names:
        digest = hashlib.sha256(name.encode()).hexdigest()[:32]
        handle = (lock_dir / f"{digest}.lock").open("a+b")
        handles.append(handle)
        while True:
            global _RECEIVED_SIGNAL
            if _RECEIVED_SIGNAL is not None:
                return handles, False
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    _write_state(
                        state_path,
                        status="timed_out",
                        exit_code=124,
                        timeout_stage="resource_lock",
                        finished_at=time.time(),
                    )
                    return handles, False
                if _cancelled(cancel_path):
                    _RECEIVED_SIGNAL = signal.SIGTERM
                    return handles, False
                time.sleep(0.05)
    return handles, True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: job_runner.py SPEC.json", file=sys.stderr)
        return 2

    spec_path = Path(args[0])
    spec = json.loads(spec_path.read_text())
    job_id = str(spec["id"])
    state_id = str(spec["state_id"])
    state_path = Path(spec["state_path"])
    cancel_path = Path(spec["cancel_path"])
    lock_dir = Path(spec["lock_dir"])
    timeout_seconds = max(1, min(int(spec["timeout_seconds"]), 604800))
    lock_wait_seconds = max(0, min(int(spec.get("resource_lock_wait_seconds", 30)), 604800))
    lock_names = sorted({str(x) for x in spec.get("resource_locks", [])})

    os.environ[STATE_ENV] = state_id
    os.environ[JOB_ENV] = job_id
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _on_signal)

    started = time.time()
    _write_state(
        state_path,
        id=job_id,
        status="waiting_for_lock" if lock_names else "running",
        started_at=started,
        runner_pid=os.getpid(),
        timeout_stage=None,
        interrupted_reason=None,
    )

    lock_handles: list[Any] = []
    try:
        lock_handles, acquired = _acquire_locks(
            lock_names, lock_dir, lock_wait_seconds, state_path, cancel_path
        )
        if not acquired:
            if _RECEIVED_SIGNAL is not None:
                return _terminal_from_signal(state_path, cancel_path, _RECEIVED_SIGNAL)
            return 124 if _read_json(state_path).get("status") == "timed_out" else 1

        if _RECEIVED_SIGNAL is not None:
            return _terminal_from_signal(state_path, cancel_path, _RECEIVED_SIGNAL)

        running_at = time.time()
        _write_state(state_path, status="running", running_at=running_at)
        child = subprocess.Popen(
            [str(spec["shell"]), "-c", str(spec["command"])],
            cwd=str(spec["cwd"]),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
        )
        _write_state(state_path, child_pid=child.pid)
        deadline = time.monotonic() + timeout_seconds

        while True:
            rc = child.poll()
            if _RECEIVED_SIGNAL is not None:
                signum = _RECEIVED_SIGNAL
                _cleanup_marked(state_id, job_id, signum)
                return _terminal_from_signal(state_path, cancel_path, signum)

            if _cancelled(cancel_path):
                _cleanup_marked(state_id, job_id, signal.SIGTERM)
                _write_state(
                    state_path,
                    status="cancelled",
                    exit_code=143,
                    finished_at=time.time(),
                )
                return 143

            if rc is not None:
                _cleanup_marked(state_id, job_id)
                _write_state(
                    state_path,
                    status="finished" if rc == 0 else "failed",
                    exit_code=int(rc),
                    finished_at=time.time(),
                )
                return 0

            if time.monotonic() >= deadline:
                _cleanup_marked(state_id, job_id, signal.SIGTERM)
                _write_state(
                    state_path,
                    status="timed_out",
                    exit_code=124,
                    timeout_stage="execution",
                    finished_at=time.time(),
                )
                return 124

            time.sleep(0.05)
    except Exception as exc:
        _cleanup_marked(state_id, job_id)
        _write_state(
            state_path,
            status="failed",
            exit_code=127,
            finished_at=time.time(),
            interrupted_reason=f"runner_error:{type(exc).__name__}",
        )
        print(f"job runner failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        for handle in reversed(lock_handles):
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                handle.close()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
