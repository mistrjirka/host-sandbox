import tempfile
import threading
import time
import unittest
from pathlib import Path

from host_sandbox.agent_transport import AgentRegistry
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

    def test_duplicate_name_replaces_old_agent(self):
        registry = AgentRegistry()
        first = registry.register("pc", "one")
        second = registry.register("pc", "two")
        self.assertNotEqual(first["agent_id"], second["agent_id"])
        self.assertTrue(registry.is_online("pc"))
        with self.assertRaises(KeyError):
            registry.poll(first["agent_id"], 0)


if __name__ == "__main__":
    unittest.main()
