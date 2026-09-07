from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PendingCall:
    id: str
    method: str
    params: dict[str, Any]
    created_at: float
    expires_at: float
    event: threading.Event = field(default_factory=threading.Event, repr=False)
    response: dict[str, Any] | None = None


@dataclass
class AgentState:
    name: str
    agent_id: str
    instance_id: str
    registered_at: float
    last_seen: float
    system_info: dict[str, Any]
    queue: deque[PendingCall] = field(default_factory=deque, repr=False)
    pending: dict[str, PendingCall] = field(default_factory=dict, repr=False)
    condition: threading.Condition = field(default_factory=threading.Condition, repr=False)


DEFAULT_AGENT_LEASE_SECONDS = 60
DEFAULT_REPLACEMENT_GRACE_SECONDS = 15
DEFAULT_AGENT_CALL_TIMEOUT_SECONDS = 24


class AgentRegistry:
    """In-memory registry for foreground host agents using outbound long polling."""

    def __init__(
        self,
        *,
        lease_seconds: int = DEFAULT_AGENT_LEASE_SECONDS,
        replacement_grace_seconds: int = DEFAULT_REPLACEMENT_GRACE_SECONDS,
    ) -> None:
        self.lease_seconds = max(10, int(lease_seconds))
        self.replacement_grace_seconds = max(1, min(self.lease_seconds, int(replacement_grace_seconds)))
        self._lock = threading.RLock()
        self._by_name: dict[str, AgentState] = {}
        self._by_id: dict[str, AgentState] = {}

    def register(self, name: str, instance_id: str, system_info: dict[str, Any] | None = None) -> dict[str, Any]:
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError("agent name must contain 1-100 characters")
        instance_id = instance_id or uuid.uuid4().hex
        now = time.time()
        with self._lock:
            old = self._by_name.get(name)
            if old is not None:
                if old.instance_id == instance_id:
                    # A foreground agent deliberately keeps instance_id stable
                    # while re-registering after transient poll/HTTP failures.
                    # Resume the existing state so queued and in-flight calls are
                    # not failed merely because the same physical client retried.
                    old.last_seen = now
                    if system_info is not None:
                        old.system_info = dict(system_info)
                    return {
                        "agent_id": old.agent_id,
                        "name": old.name,
                        "lease_seconds": self.lease_seconds,
                    }
                if (now - old.last_seen) <= self.replacement_grace_seconds:
                    # Never let a second physical client with the same host name
                    # evict a recently polling connection. Keep this guard shorter
                    # than the availability lease: logical sessions tolerate a
                    # transient network gap for longer, while a genuinely crashed
                    # connector can still be replaced promptly.
                    raise RuntimeError(
                        f"agent name {name!r} is already connected by another live instance"
                    )
                self._drop_locked(old, reason="stale client replaced by a new connection")

            state = AgentState(
                name=name,
                agent_id="a_" + uuid.uuid4().hex[:24],
                instance_id=instance_id,
                registered_at=now,
                last_seen=now,
                system_info=dict(system_info or {}),
            )
            self._by_name[name] = state
            self._by_id[state.agent_id] = state
        return {"agent_id": state.agent_id, "name": state.name, "lease_seconds": self.lease_seconds}

    def _drop_locked(self, state: AgentState, reason: str) -> None:
        self._by_name.pop(state.name, None)
        self._by_id.pop(state.agent_id, None)
        with state.condition:
            for call in list(state.pending.values()):
                if call.response is None:
                    call.response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": f"agent disconnected: {reason}"}}
                    call.event.set()
            state.condition.notify_all()

    def disconnect(self, agent_id: str) -> None:
        with self._lock:
            state = self._by_id.get(agent_id)
            if state is not None:
                self._drop_locked(state, reason="client stopped")

    def _state_by_id(self, agent_id: str) -> AgentState:
        with self._lock:
            state = self._by_id.get(agent_id)
        if state is None:
            raise KeyError("unknown agent_id")
        return state

    def _online(self, state: AgentState, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return (now - state.last_seen) <= self.lease_seconds

    def is_online(self, name: str) -> bool:
        with self._lock:
            state = self._by_name.get(name)
        return bool(state and self._online(state))

    def names(self) -> list[str]:
        now = time.time()
        with self._lock:
            return sorted(name for name, state in self._by_name.items() if self._online(state, now))

    def list_agents(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            states = list(self._by_name.values())
        return [
            {
                "name": s.name,
                "online": self._online(s, now),
                "connected": True,
                "transport": "outbound-agent",
                "instance_id": s.instance_id,
                "registered_at": s.registered_at,
                "last_seen": s.last_seen,
                "system_info": s.system_info,
            }
            for s in sorted(states, key=lambda x: x.name)
            if self._online(s, now)
        ]

    def poll(self, agent_id: str, wait_seconds: int = 20) -> dict[str, Any] | None:
        state = self._state_by_id(agent_id)
        deadline = time.monotonic() + max(0, min(int(wait_seconds), 25))
        with state.condition:
            state.last_seen = time.time()
            while True:
                now = time.time()
                while state.queue:
                    call = state.queue.popleft()
                    # A requester may have timed out while this host was offline/busy.
                    # Never deliver orphaned or expired calls to the computer later.
                    if call.id not in state.pending or call.expires_at <= now:
                        state.pending.pop(call.id, None)
                        continue
                    state.last_seen = now
                    return {
                        "call_id": call.id,
                        "method": call.method,
                        "params": call.params,
                        "expires_at": call.expires_at,
                    }
                left = deadline - time.monotonic()
                if left <= 0:
                    state.last_seen = time.time()
                    return None
                state.condition.wait(timeout=left)

    def submit_result(self, agent_id: str, call_id: str, response: dict[str, Any]) -> None:
        state = self._state_by_id(agent_id)
        with state.condition:
            state.last_seen = time.time()
            call = state.pending.get(call_id)
            if call is None:
                raise KeyError("unknown call_id")
            call.response = response
            call.event.set()
            state.condition.notify_all()

    def request(
        self,
        name: str,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_AGENT_CALL_TIMEOUT_SECONDS,
    ) -> Any:
        with self._lock:
            state = self._by_name.get(name)
        if state is None or not self._online(state):
            raise RuntimeError(f"host agent {name!r} is not connected; start host-sandbox connect on that computer")
        timeout = max(1, int(timeout_seconds))
        created_at = time.time()
        call = PendingCall(
            "c_" + uuid.uuid4().hex[:24], method, dict(params or {}), created_at, created_at + timeout
        )
        with state.condition:
            state.pending[call.id] = call
            state.queue.append(call)
            state.condition.notify_all()
        try:
            if not call.event.wait(timeout=timeout):
                raise TimeoutError(f"host agent {name!r} did not answer {method!r} within {timeout_seconds}s")
            response = call.response or {}
            if "error" in response:
                raise RuntimeError(str(response["error"]))
            return response.get("result")
        finally:
            with state.condition:
                state.pending.pop(call.id, None)
                # If the agent has not polled this call yet, remove it from the
                # delivery queue as well. deque.remove is safe under condition.
                try:
                    state.queue.remove(call)
                except ValueError:
                    pass
                state.condition.notify_all()


def agent_request_timeout(method: str, params: dict[str, Any] | None = None) -> int:
    """Keep the router response comfortably inside the tunnel response deadline.

    Structured exec calls intentionally return a durable job after wait_seconds;
    there is no reason for the hub to hold its HTTP/MCP request for minutes when
    an outbound agent stops polling.
    """
    if method != "tools/call":
        return 12
    payload = params or {}
    name = str(payload.get("name") or "")
    arguments = payload.get("arguments") or {}
    if name == "exec_command" and isinstance(arguments, dict):
        try:
            wait = max(0, min(20, int(arguments.get("wait_seconds", 8))))
        except (TypeError, ValueError):
            wait = 8
        return min(DEFAULT_AGENT_CALL_TIMEOUT_SECONDS, max(10, wait + 5))
    if name == "exec_commands" and isinstance(arguments, dict):
        waits = []
        for item in arguments.get("commands") or []:
            if not isinstance(item, dict):
                continue
            try:
                waits.append(max(0, min(20, int(item.get("wait_seconds", 8)))))
            except (TypeError, ValueError):
                waits.append(8)
        wait = max(waits, default=8)
        return min(DEFAULT_AGENT_CALL_TIMEOUT_SECONDS, max(10, wait + 5))
    return DEFAULT_AGENT_CALL_TIMEOUT_SECONDS


class AgentMCPClient:
    def __init__(self, registry: AgentRegistry, name: str) -> None:
        self.registry = registry
        self.name = name

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return self.registry.request(
            self.name, method, params, timeout_seconds=agent_request_timeout(method, params)
        )

    def close(self) -> None:
        pass
