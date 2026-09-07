import tempfile
import threading
import time
import unittest
from pathlib import Path

from host_sandbox.agent_transport import AgentMCPClient, AgentRegistry, agent_request_timeout
from host_sandbox.audit import AuditLog
from host_sandbox.core import HostTools
from host_sandbox.mcp import MCPServer
from host_sandbox.router_mcp import RouterMCP
from host_sandbox.ssh_router import SSHRouter


class AgentTransportTests(unittest.TestCase):
    def test_foreground_agent_becomes_dynamic_project_and_executes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = root / "hosts.json"
            cfg.write_text('{"hosts":[{"name":"hub","local":true}]}')
            registry = AgentRegistry(lease_seconds=15)
            router = SSHRouter(cfg, registry)
            aggregate = RouterMCP(router, root / "sessions.json")
            audit = AuditLog(root / "agent-state")
            tools = HostTools(audit, root / "agent-state", str(root))
            mcp = MCPServer(tools, audit)
            registration = registry.register("rtx3090", "instance-test", {"hostname":"pc"})
            agent_id = registration["agent_id"]
            stop = threading.Event()

            def agent_loop():
                while not stop.is_set():
                    call = registry.poll(agent_id, 1)
                    if call is None:
                        continue
                    response = mcp.handle({"jsonrpc":"2.0","id":call["call_id"],"method":call["method"],"params":call["params"]})
                    registry.submit_result(agent_id, call["call_id"], response)

            thread = threading.Thread(target=agent_loop, daemon=True)
            thread.start()
            try:
                projects = aggregate.call("list_projects", {})["projects"]
                self.assertIn("rtx3090", [p["id"] for p in projects])
                self.assertEqual(next(p for p in projects if p["id"] == "rtx3090")["transport"], "outbound-agent")
                session = aggregate.call("create_session", {"project":"rtx3090"})
                result = aggregate.call("exec_command", {"session_id":session["id"], "command":"printf foreground", "wait_seconds":2})
                self.assertEqual(result["exit_code"], 0)
                self.assertIn("foreground", result["output_tail"])
                registry.disconnect(agent_id)
                self.assertNotIn("rtx3090", [p["id"] for p in aggregate.call("list_projects", {})["projects"]])
            finally:
                stop.set(); thread.join(timeout=2); tools.close(); router.close()


    def test_timed_out_call_is_removed_from_delivery_queue(self):
        registry = AgentRegistry()
        registration = registry.register("pc", "one")
        agent_id = registration["agent_id"]
        outcome = {}

        def requester():
            try:
                registry.request("pc", "tools/call", {"name": "exec_command"}, timeout_seconds=1)
            except Exception as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=requester)
        thread.start()
        time.sleep(0.05)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome.get("error"), TimeoutError)
        self.assertIsNone(registry.poll(agent_id, 0))

    def test_polled_call_carries_expiry_deadline(self):
        registry = AgentRegistry()
        registration = registry.register("pc", "one")
        agent_id = registration["agent_id"]
        outcome = {}

        def requester():
            outcome["result"] = registry.request("pc", "tools/list", {}, timeout_seconds=2)

        thread = threading.Thread(target=requester)
        thread.start()
        deadline = time.time() + 1
        call = None
        while time.time() < deadline and call is None:
            call = registry.poll(agent_id, 0)
            if call is None:
                time.sleep(0.01)
        self.assertIsNotNone(call)
        assert call is not None
        self.assertGreater(call["expires_at"], time.time())
        registry.submit_result(agent_id, call["call_id"], {"jsonrpc": "2.0", "id": call["call_id"], "result": {"ok": True}})
        thread.join(timeout=2)
        self.assertEqual(outcome.get("result"), {"ok": True})

    def test_same_instance_reregistration_resumes_without_dropping_pending_call(self):
        registry = AgentRegistry()
        first = registry.register("pc", "one", {"generation": 1})
        outcome = {}

        def requester():
            try:
                outcome["result"] = registry.request("pc", "tools/list", {}, timeout_seconds=2)
            except Exception as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=requester)
        thread.start()
        deadline = time.time() + 1
        while time.time() < deadline:
            with registry._lock:
                state = registry._by_name["pc"]
                if state.pending:
                    break
            time.sleep(0.01)

        resumed = registry.register("pc", "one", {"generation": 2})
        self.assertEqual(first["agent_id"], resumed["agent_id"])
        call = registry.poll(resumed["agent_id"], 0)
        self.assertIsNotNone(call)
        assert call is not None
        registry.submit_result(
            resumed["agent_id"], call["call_id"],
            {"jsonrpc": "2.0", "id": call["call_id"], "result": {"ok": True}},
        )
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome.get("result"), {"ok": True})
        self.assertEqual(registry.list_agents()[0]["system_info"], {"generation": 2})

    def test_different_live_instance_cannot_replace_existing_agent(self):
        registry = AgentRegistry()
        first = registry.register("pc", "one")
        with self.assertRaisesRegex(RuntimeError, "already connected by another live instance"):
            registry.register("pc", "two")
        self.assertTrue(registry.is_online("pc"))
        self.assertIsNone(registry.poll(first["agent_id"], 0))


    def test_default_lease_tolerates_short_poll_outage(self):
        registry = AgentRegistry()
        registry.register("pc", "one")
        with registry._lock:
            state = registry._by_name["pc"]
            state.last_seen = time.time() - 30
        self.assertTrue(registry.is_online("pc"))
        with registry._lock:
            registry._by_name["pc"].last_seen = time.time() - 61
        self.assertFalse(registry.is_online("pc"))

    def test_replacement_guard_is_shorter_than_availability_lease(self):
        registry = AgentRegistry()
        first = registry.register("pc", "one")
        with registry._lock:
            registry._by_name["pc"].last_seen = time.time() - 16
        self.assertTrue(registry.is_online("pc"))
        second = registry.register("pc", "two")
        self.assertNotEqual(first["agent_id"], second["agent_id"])

    def test_agent_request_timeout_stays_below_tunnel_window(self):
        self.assertEqual(agent_request_timeout("tools/list", {}), 12)
        self.assertEqual(agent_request_timeout("tools/call", {"name": "exec_command", "arguments": {"wait_seconds": 0}}), 10)
        self.assertEqual(agent_request_timeout("tools/call", {"name": "exec_command", "arguments": {"wait_seconds": 20}}), 24)
        self.assertEqual(agent_request_timeout("tools/call", {"name": "read_file", "arguments": {}}), 24)

    def test_different_instance_can_replace_expired_lease(self):
        registry = AgentRegistry(lease_seconds=10)
        first = registry.register("pc", "one")
        with registry._lock:
            registry._by_name["pc"].last_seen = time.time() - 11
        second = registry.register("pc", "two")
        self.assertNotEqual(first["agent_id"], second["agent_id"])
        self.assertTrue(registry.is_online("pc"))
        with self.assertRaises(KeyError):
            registry.poll(first["agent_id"], 0)


if __name__ == "__main__":
    unittest.main()
