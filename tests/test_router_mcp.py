import tempfile
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
    def test_mcp_single_endpoint_surface(self):
        m=RouterMCP(FakeRouter())
        listed=m.handle({"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}})
        names={t["name"] for t in listed["result"]["tools"]}
        self.assertIn("create_session", names)
        self.assertIn("exec_command", names)
        self.assertIn("read_file", names)
        self.assertNotIn("host", listed["result"]["tools"][0]["inputSchema"].get("properties",{}))

if __name__ == '__main__': unittest.main()
