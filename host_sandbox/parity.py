from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


def _clamp_int(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _image_mime(path: Path, data: bytes) -> str | None:
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed and guessed.startswith("image/"):
        return guessed
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    head = data[:1024].lstrip().lower()
    if head.startswith(b"<svg") or b"<svg" in head:
        return "image/svg+xml"
    return None


class ParityHostToolsMixin:
    """Host-side primitives used to mirror Development Sandbox conveniences."""

    def read_text_lines(self, path: str, start_line: int = 1, max_lines: int = 1000) -> dict[str, Any]:
        p = self._path(path)  # type: ignore[attr-defined]
        start = max(1, int(start_line))
        limit = _clamp_int(max_lines, 1, 20_000)
        selected: list[tuple[int, str]] = []
        total = 0
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for total, line in enumerate(f, 1):
                if total >= start and len(selected) < limit:
                    selected.append((total, line.rstrip("\n")))
        end = selected[-1][0] if selected else min(total, start - 1)
        has_more = end < total
        return {
            "path": str(p), "start_line": start, "end_line": end, "total_lines": total,
            "has_more": has_more, "eof": not has_more, "next_start_line": end + 1 if has_more else None,
            "content": "\n".join(f"{n}: {line}" for n, line in selected),
        }

    def read_binary_file(self, path: str, max_bytes: int = 16_777_216) -> dict[str, Any]:
        p = self._path(path)  # type: ignore[attr-defined]
        limit = _clamp_int(max_bytes, 1, 16_777_216)
        size = p.stat().st_size
        if size > limit:
            raise ValueError(f"file is {size} bytes; max_bytes is {limit}")
        data = p.read_bytes()
        mime, _ = mimetypes.guess_type(str(p))
        return {
            "path": str(p),
            "bytes": len(data),
            "mime_type": mime or "application/octet-stream",
            "sha256": hashlib.sha256(data).hexdigest(),
            "content_base64": base64.b64encode(data).decode("ascii"),
        }

    def view_image(self, path: str, max_bytes: int = 16_777_216) -> dict[str, Any]:
        p = self._path(path)  # type: ignore[attr-defined]
        limit = _clamp_int(max_bytes, 1, 16_777_216)
        size = p.stat().st_size
        if size > limit:
            raise ValueError(f"image is {size} bytes; max_bytes is {limit}")
        data = p.read_bytes()
        mime = _image_mime(p, data)
        if not mime:
            raise ValueError(f"unsupported or unrecognized image format: {p}")
        return {
            "path": str(p),
            "bytes": len(data),
            "mime_type": mime,
            "sha256": hashlib.sha256(data).hexdigest(),
            "content_base64": base64.b64encode(data).decode("ascii"),
        }

    def write_binary_file(
        self,
        path: str,
        content_base64: str,
        create_parents: bool = True,
        overwrite: bool = True,
    ) -> dict[str, Any]:
        p = self._path(path)  # type: ignore[attr-defined]
        data = base64.b64decode(content_base64, validate=True)
        if len(data) > 16_777_216:
            raise ValueError("decoded binary exceeds 16 MiB")
        if p.exists() and not overwrite:
            raise FileExistsError(str(p))
        if create_parents:
            p.parent.mkdir(parents=True, exist_ok=True)
        mode = "wb" if overwrite else "xb"
        with p.open(mode) as f:
            f.write(data)
        return {
            "path": str(p),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "overwritten": bool(overwrite),
        }

    def apply_patch(self, repo: str, patch: str, check_only: bool = False) -> dict[str, Any]:
        root = self._path(repo)  # type: ignore[attr-defined]
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        if len(patch.encode("utf-8")) > 8_388_608:
            raise ValueError("patch exceeds 8 MiB")
        args = ["git", "-C", str(root), "apply"]
        if check_only:
            args.append("--check")
        proc = subprocess.run(args, input=patch.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(stderr.strip() or stdout.strip() or f"git apply exited {proc.returncode}")
        return {"repo": str(root), "check_only": bool(check_only), "applied": not check_only, "stdout": stdout, "stderr": stderr}

    @staticmethod
    def _git(root: Path, args: list[str]) -> str:
        proc = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip() or f"git {' '.join(args)} failed")
        return proc.stdout.decode("utf-8", errors="replace")

    def git_status_diff(
        self,
        repo: str = ".",
        max_total_chars: int = 200_000,
        include_untracked: bool = True,
        include_diff: bool = True,
        paths: list[str] | None = None,
    ) -> dict[str, Any]:
        root = self._path(repo)  # type: ignore[attr-defined]
        limit = _clamp_int(max_total_chars, 1000, 2_000_000)
        selected = [str(p) for p in (paths or [])][:200]
        path_args = ["--", *selected] if selected else []
        branch = self._git(root, ["branch", "--show-current"]).strip()
        status = self._git(root, ["status", "--short", "--branch", *path_args])
        unstaged = self._git(root, ["diff", "--no-ext-diff", *path_args]) if include_diff else ""
        staged = self._git(root, ["diff", "--cached", "--no-ext-diff", *path_args]) if include_diff else ""
        if include_diff:
            ws = subprocess.run(["git", "-C", str(root), "diff", "--check", *path_args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            whitespace = (ws.stdout + ws.stderr).decode("utf-8", errors="replace")
        else:
            whitespace = ""
        untracked = ""
        if include_untracked:
            untracked = self._git(root, ["ls-files", "--others", "--exclude-standard", *path_args])
        pieces = {
            "status": status,
            "diff": unstaged,
            "staged_diff": staged,
            "untracked": untracked,
            "whitespace_errors": whitespace,
        }
        remaining = limit
        truncated_sections: list[str] = []
        order = ("status", "diff", "staged_diff", "untracked", "whitespace_errors")
        for key in order:
            value = pieces[key]
            if remaining <= 0:
                if value:
                    pieces[key] = ""; truncated_sections.append(key)
                continue
            if len(value) > remaining:
                pieces[key] = value[:remaining]
                truncated_sections.append(key); remaining = 0
            else:
                remaining -= len(value)
        returned = sum(len(pieces[key]) for key in order)
        return {
            "repo": str(root), "branch": branch, **pieces,
            "status_truncated": "status" in truncated_sections,
            "returned_chars": returned, "truncated": bool(truncated_sections),
            "truncated_sections": truncated_sections, "max_total_chars": limit,
        }

    def _terminal_name(self, terminal_id: str) -> str:
        digest = hashlib.sha256(terminal_id.encode()).hexdigest()[:16]
        return f"host-sandbox-{digest}"

    def _terminal_log(self, terminal_id: str) -> Path:
        root = self.state_dir / "terminals"  # type: ignore[attr-defined]
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{hashlib.sha256(terminal_id.encode()).hexdigest()[:16]}.log"

    def _ensure_terminal(self, terminal_id: str) -> tuple[str, Path]:
        if not shutil.which("tmux"):
            raise RuntimeError("tmux is required for interactive terminal tools")
        name = self._terminal_name(terminal_id)
        log = self._terminal_log(terminal_id)
        exists = subprocess.run(["tmux", "has-session", "-t", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if not exists:
            shell = os.environ.get("SHELL") or "/bin/bash"
            subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", self.cwd, shell], check=True)  # type: ignore[attr-defined]
            quoted = shlex.quote(str(log))
            subprocess.run(["tmux", "pipe-pane", "-t", f"{name}:0.0", "-o", f"cat >> {quoted}"], check=True)
        return name, log

    def read_terminal(
        self,
        terminal_id: str,
        cursor: int | None = None,
        max_bytes: int = 131_072,
        wait_seconds: int = 0,
        screen_lines: int = 200,
    ) -> dict[str, Any]:
        name, log = self._ensure_terminal(terminal_id)
        start = max(0, int(cursor or 0))
        limit = _clamp_int(max_bytes, 1000, 2_097_152)
        wait = _clamp_int(wait_seconds, 0, 25)
        deadline = time.monotonic() + wait
        while wait and (not log.exists() or log.stat().st_size <= start) and time.monotonic() < deadline:
            time.sleep(0.1)
        size = log.stat().st_size if log.exists() else 0
        if start > size:
            start = size
        with log.open("rb") if log.exists() else open(os.devnull, "rb") as f:
            f.seek(start)
            data = f.read(limit)
        next_cursor = start + len(data)
        more = next_cursor < size
        screen_n = _clamp_int(screen_lines, 1, 10_000)
        capture = subprocess.run(
            ["tmux", "capture-pane", "-p", "-J", "-t", f"{name}:0.0", "-S", f"-{screen_n}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout.decode("utf-8", errors="replace").rstrip("\n")
        current_command = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{name}:0.0", "#{pane_current_command}"],
            stdout=subprocess.PIPE, check=True,
        ).stdout.decode().strip()
        pane_dead = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{name}:0.0", "#{pane_dead}"],
            stdout=subprocess.PIPE, check=True,
        ).stdout.decode().strip() == "1"
        return {
            "terminal_id": terminal_id,
            "running": True,
            "pane_dead": pane_dead,
            "current_command": current_command,
            "start_cursor": start,
            "next_cursor": next_cursor,
            "more_output_available": more,
            "output": data.decode("utf-8", errors="replace"),
            "screen": capture,
            "screen_lines_returned": len(capture.splitlines()) if capture else 0,
            "screen_hash": hashlib.sha256(capture.encode()).hexdigest()[:16],
        }

    def send_terminal_input(self, terminal_id: str, text: str, press_enter: bool = True) -> dict[str, Any]:
        name, _ = self._ensure_terminal(terminal_id)
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:0.0", "-l", text], check=True)
        if press_enter:
            subprocess.run(["tmux", "send-keys", "-t", f"{name}:0.0", "Enter"], check=True)
        return {"terminal_id": terminal_id, "characters": len(text), "press_enter": bool(press_enter), "sent": True}

    def send_terminal_key(self, terminal_id: str, key: str) -> dict[str, Any]:
        name, _ = self._ensure_terminal(terminal_id)
        mapping = {
            "C-c": "C-c", "C-d": "C-d", "C-z": "C-z", "Enter": "Enter", "Tab": "Tab",
            "Escape": "Escape", "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right", "BSpace": "BSpace",
        }
        if key not in mapping:
            raise ValueError(f"unsupported terminal key: {key}")
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:0.0", mapping[key]], check=True)
        return {"terminal_id": terminal_id, "key": key, "sent": True}
