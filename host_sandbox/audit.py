from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, state_dir: Path, max_events: int = 2000) -> None:
        self._lock = threading.RLock()
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._next_id = 1
        self._state_dir = state_dir
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._state_dir / "events.jsonl"
        self.started_at = time.time()
        self.tool_calls = 0
        self.errors = 0

    def add(self, kind: str, summary: str, *, status: str = "info", **data: Any) -> dict[str, Any]:
        with self._lock:
            event = {
                "id": self._next_id,
                "ts": time.time(),
                "kind": kind,
                "summary": summary,
                "status": status,
                "data": data,
            }
            self._next_id += 1
            if kind == "tool":
                self.tool_calls += 1
            if status == "error":
                self.errors += 1
            self._events.append(event)
            try:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            except OSError:
                pass
            return event

    def after(self, event_id: int = 0, limit: int = 300) -> list[dict[str, Any]]:
        with self._lock:
            values = [event for event in self._events if int(event["id"]) > event_id]
            return values[-max(1, min(limit, 1000)) :]

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)[-max(1, min(limit, 1000)) :]

    @property
    def latest_id(self) -> int:
        with self._lock:
            return self._next_id - 1
