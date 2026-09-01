import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from host_sandbox.router_mcp import RouterMCP
from host_sandbox.ssh_router import HostConfig


class FakeRouter:
    def __init__(self):
        self.hosts = {"alpha": HostConfig("alpha", "alpha.invalid"), "beta": HostConfig("beta", "beta.invalid")}
        self.calls = []
    def list_hosts(self):
        return {"hosts": [{"name":"alpha","online":True},{"name":"beta","online":True}]}
    def has_host(self, name):
        return name in self.hosts
    def project_entries(self):
        return [{"id":name,"name":name,"description":"test host","transport":"mock"} for name in sorted(self.hosts)]
    def call_tool(self, host, name, arguments):
        self.calls.append((host,name,arguments))
        if name == "exec_command":
            return {"structuredContent": {"id":"j_1234567890abcdef","status":"finished","exit_code":0,"output_tail":"ok"}}
        if name == "write_file":
            return {"structuredContent": {"path":arguments["path"],"characters":len(arguments["content"])}}
        return {"structuredContent": {"host":host,"tool":name,"arguments":arguments}}


class RouterMCPTests(unittest.TestCase):
    def test_sessions_select_host_and_proxy(self):
        with tempfile.TemporaryDirectory() as td:
            router=FakeRouter(); m=RouterMCP(router, Path(td)/"sessions.json")
            projects=m.call("list_projects", {})
            self.assertEqual([p["id"] for p in projects["projects"]], ["alpha","beta"])
            session=m.call("create_session", {"project":"beta","label":"work"})
            sid=session["id"]
            r=m.call("exec_command", {"session_id":sid,"command":"printf ok","cwd":"."})
            self.assertEqual(r["exit_code"],0)
            self.assertEqual(router.calls[-1][0],"beta")
            w=m.call("write_file", {"session_id":sid,"repo":"repo","path":"x.txt","content":"abc"})
            self.assertEqual(router.calls[-1][2]["path"],"repo/x.txt")
            self.assertTrue((Path(td)/"sessions.json").exists())
    def test_exec_commands_routes_per_item_session(self):
        with tempfile.TemporaryDirectory() as td:
            router=FakeRouter(); m=RouterMCP(router, Path(td)/"sessions.json")
            alpha=m.call("create_session", {"project":"alpha"})["id"]
            beta=m.call("create_session", {"project":"beta"})["id"]
            result=m.call("exec_commands", {
                "commands": [
                    {"session_id":alpha, "command":"printf alpha", "wait_seconds":2},
                    {"session_id":beta, "command":"printf beta", "wait_seconds":2},
                ],
                "concurrency": 2,
            })
            self.assertEqual(len(result["results"]), 2)
            exec_hosts=[host for host,name,_ in router.calls if name == "exec_command"]
            self.assertEqual(set(exec_hosts), {"alpha", "beta"})

    def test_resource_locks_serialize_same_host_but_not_other_hosts(self):
        class SlowRouter(FakeRouter):
            def __init__(self):
                super().__init__(); self.timeline=[]; self.guard=threading.Lock()
            def call_tool(self, host, name, arguments):
                if name == "exec_command":
                    with self.guard: self.timeline.append((host, "start", time.monotonic()))
                    time.sleep(0.18)
                    with self.guard: self.timeline.append((host, "end", time.monotonic()))
                    return {"structuredContent": {"exit_code":0,"output_tail":host}}
                return super().call_tool(host,name,arguments)

        with tempfile.TemporaryDirectory() as td:
            router=SlowRouter(); m=RouterMCP(router, Path(td)/"sessions.json")
            alpha1=m.call("create_session", {"project":"alpha"})["id"]
            alpha2=m.call("create_session", {"project":"alpha"})["id"]
            beta=m.call("create_session", {"project":"beta"})["id"]
            started=time.monotonic()
            result=m.call("exec_commands", {
                "commands":[
                    {"session_id":alpha1,"command":"a1","resource_locks":["gpu:all"]},
                    {"session_id":alpha2,"command":"a2","resource_locks":["gpu:all"]},
                    {"session_id":beta,"command":"b","resource_locks":["gpu:all"]},
                ],
                "concurrency":3,
            })
            elapsed=time.monotonic()-started
            self.assertEqual(len(result["results"]),3)
            # Two alpha calls sharing gpu:all must serialize (~0.36s total), while beta may overlap.
            self.assertGreater(elapsed,0.33)
            self.assertLess(elapsed,0.52)
            alpha_starts=[t for h,e,t in router.timeline if h=="alpha" and e=="start"]
            alpha_ends=[t for h,e,t in router.timeline if h=="alpha" and e=="end"]
            self.assertGreaterEqual(max(alpha_starts), min(alpha_ends)-0.02)
            beta_start=next(t for h,e,t in router.timeline if h=="beta" and e=="start")
            self.assertLess(beta_start, min(alpha_ends))

    def test_resource_lock_schema_matches_development_sandbox(self):
        m=RouterMCP(FakeRouter())
        listed=m.handle({"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}})
        exec_tool=next(t for t in listed["result"]["tools"] if t["name"]=="exec_command")
        locks=exec_tool["inputSchema"]["properties"]["resource_locks"]
        self.assertEqual(locks["type"],"array")
        self.assertEqual(locks["maxItems"],16)
        batch=next(t for t in listed["result"]["tools"] if t["name"]=="exec_commands")
        item_locks=batch["inputSchema"]["properties"]["commands"]["items"]["properties"]["resource_locks"]
        self.assertEqual(item_locks["maxItems"],16)

    def test_mcp_single_endpoint_surface(self):
        m=RouterMCP(FakeRouter())
        listed=m.handle({"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}})
        names={t["name"] for t in listed["result"]["tools"]}
        self.assertIn("create_session", names)
        self.assertIn("exec_command", names)
        self.assertIn("read_file", names)
        self.assertNotIn("host", listed["result"]["tools"][0]["inputSchema"].get("properties",{}))

    def test_sdk_style_tool_metadata_and_modern_discover(self):
        mcp = RouterMCP(FakeRouter())
        tool = next(t for t in mcp.handle({"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}})["result"]["tools"] if t["name"] == "list_jobs")
        self.assertEqual(tool["title"], "List command jobs")
        self.assertEqual(tool["inputSchema"]["title"], "list_jobsArguments")
        self.assertEqual(tool["inputSchema"]["properties"]["session_id"]["title"], "Session Id")
        self.assertEqual(tool["inputSchema"]["properties"]["limit"]["title"], "Limit")
        self.assertEqual(tool["outputSchema"]["type"], "object")
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        discover = mcp.handle({"jsonrpc":"2.0","id":2,"method":"server/discover","params":{}})["result"]
        self.assertEqual(discover["supportedVersions"], ["2026-07-28"])
        self.assertEqual(discover["resultType"], "complete")
        self.assertEqual(discover["cacheScope"], "private")
        self.assertIn("io.modelcontextprotocol/serverInfo", discover["_meta"])

if __name__ == '__main__': unittest.main()
