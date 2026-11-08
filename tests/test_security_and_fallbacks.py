"""Regressions for host-control HTTP boundaries and filesystem fallbacks."""
import http.client
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from host_sandbox.agent_client import run_connected_agent
from host_sandbox.agent_server import AgentHTTPServer
from host_sandbox.agent_transport import AgentRegistry
from host_sandbox.audit import AuditLog
from host_sandbox.core import HostTools
from host_sandbox.http_server import HostHTTPServer
from host_sandbox.mcp import MCPServer
from host_sandbox.router_mcp import RouterMCP
from host_sandbox.router_server import RouterHTTPServer


class FakeClient:
    def request(self, method, params):
        return {"method": method, "params": params}


class FakeRouter:
    def has_host(self, name):
        return name == "test"

    def project_entries(self):
        return [{"id": "test", "name": "test"}]

    def list_hosts(self):
        return {"hosts": [{"name": "test"}]}

    def client(self, name):
        if name != "test":
            raise KeyError(name)
        return FakeClient()

    def close(self):
        pass


class HTTPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audit = AuditLog(self.root / "state")
        self.tools = HostTools(self.audit, self.root / "state", str(self.root))
        self.mcp = MCPServer(self.tools, self.audit)
        self.servers = []

    def tearDown(self):
        for server, thread in reversed(self.servers):
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.tools.close()
        self.tmp.cleanup()

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}"

    def request(self, url, path, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                content = resp.read()
                return resp.status, dict(resp.headers), content
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_dashboard_has_no_command_endpoint_in_connect_mode(self):
        server = HostHTTPServer(("127.0.0.1", 0), self.mcp, self.tools, self.audit, "test", mcp_enabled=False)
        url = self.start(server)
        status, _, _ = self.request(url, "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"Content-Type": "application/json"})
        self.assertEqual(status, 404)
        status, _, _ = self.request(url, "/api/status")
        self.assertEqual(status, 200)
        status, _, _ = self.request(url, "/mcp", "OPTIONS")
        self.assertEqual(status, 404)

    def test_local_mcp_clients_are_unrestricted_but_browser_cross_origin_is_not(self):
        server = HostHTTPServer(("127.0.0.1", 0), self.mcp, self.tools, self.audit, "test")
        url = self.start(server)
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "system_info", "arguments": {}}}
        status, _, body = self.request(url, "/mcp", "POST", call, {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["result"]["structuredContent"]["sandboxed"])
        # A real browser cannot forge Origin; a cross-site form/fetch must not invoke tools.
        status, _, _ = self.request(url, "/mcp", "POST", call, {"Content-Type": "application/json", "Origin": "https://attacker.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.request(url, "/mcp", "POST", call, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, headers, _ = self.request(url, "/mcp", "OPTIONS", headers={"Origin": "https://attacker.example"})
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, _, _ = self.request(url, "/mcp", "POST", call, {"Content-Type": "application/json", "Origin": url})
        self.assertEqual(status, 200)
        # DNS-rebinding attacks send the attacker's domain as Host and Origin.
        status, _, _ = self.request(url, "/api/jobs", headers={"Host": "attacker.example", "Origin": "http://attacker.example"})
        self.assertEqual(status, 403)
        with patch.dict(os.environ, {"HOST_SANDBOX_ALLOWED_HOSTS": "trusted-tunnel.example"}):
            status, _, _ = self.request(url, "/health", headers={"Host": "trusted-tunnel.example"})
            self.assertEqual(status, 200)

    def test_token_protects_dashboard_data_and_frontend_can_provide_it(self):
        server = HostHTTPServer(("127.0.0.1", 0), self.mcp, self.tools, self.audit, "test", "strong-token")
        url = self.start(server)
        for path in ("/api/jobs", "/api/events", "/api/status"):
            status, _, _ = self.request(url, path)
            self.assertEqual(status, 401)
            status, _, _ = self.request(url, path, headers={"Authorization": "Bearer strong-token"})
            self.assertEqual(status, 200)
        status, _, body = self.request(url, "/")
        self.assertEqual(status, 200)
        self.assertIn(b'id="api-token"', body)
        self.assertNotIn(b"strong-token", body)

    def test_router_requires_json_and_blocks_untrusted_origin(self):
        server = RouterHTTPServer(("127.0.0.1", 0), FakeRouter(), None)
        url = self.start(server)
        call = {"jsonrpc": "2.0", "id": 9, "method": "ping", "params": {}}
        status, _, body = self.request(url, "/mcp/test", "POST", call, {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"]["method"], "ping")
        status, _, _ = self.request(url, "/mcp/test", "POST", call, {"Content-Type": "application/json", "Origin": "http://attacker.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.request(url, "/mcp/test", "POST", call, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _, _ = self.request(url, "/mcp/test", "POST", call, {"Content-Type": "application/json", "Host": "attacker.example"})
        self.assertEqual(status, 403)

    def test_router_token_protects_host_list(self):
        server = RouterHTTPServer(("127.0.0.1", 0), FakeRouter(), "strong-token")
        url = self.start(server)
        status, _, _ = self.request(url, "/hosts")
        self.assertEqual(status, 401)
        status, _, body = self.request(url, "/health")
        self.assertEqual(json.loads(body), {"ok": True})
        status, _, _ = self.request(url, "/hosts", headers={"Authorization": "Bearer strong-token"})
        self.assertEqual(status, 200)

    def test_public_bind_requires_a_token(self):
        with self.assertRaises(ValueError):
            HostHTTPServer(("0.0.0.0", 0), self.mcp, self.tools, self.audit, "test")
        with self.assertRaises(ValueError):
            RouterHTTPServer(("0.0.0.0", 0), FakeRouter(), None)

    def test_connected_dashboard_rejects_network_bind_before_registering(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            run_connected_agent(hub="http://127.0.0.1:1", token="secret", name="test", cwd=str(self.root), state_dir=self.root / "state", dashboard_bind="0.0.0.0")

    def test_body_size_is_bounded_without_reading_the_payload(self):
        server = HostHTTPServer(("127.0.0.1", 0), self.mcp, self.tools, self.audit, "test")
        self.start(server)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.putrequest("POST", "/mcp")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(64 * 1024 * 1024 + 1))
        conn.endheaders()
        response = conn.getresponse()
        self.assertEqual(response.status, 413)
        response.read()
        conn.close()

    def test_agent_listener_requires_token_and_hides_agent_metadata(self):
        with self.assertRaises(ValueError):
            AgentHTTPServer(("127.0.0.1", 0), AgentRegistry(), "")
        server = AgentHTTPServer(("127.0.0.1", 0), AgentRegistry(), "agent-secret")
        url = self.start(server)
        status, _, body = self.request(url, "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        status, _, body = self.request(url, "/health", headers={"Authorization": "Bearer agent-secret"})
        self.assertIn("agents", json.loads(body))
        status, _, _ = self.request(url, "/agent/register", "POST", {"name": "test", "instance_id": "abc"}, {"Content-Type": "application/json"})
        self.assertEqual(status, 401)


class FilesystemFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tools = HostTools(AuditLog(self.root / "state"), self.root / "state", str(self.root))

    def tearDown(self):
        self.tools.close()
        self.tmp.cleanup()

    def test_symlinks_are_inspected_removed_and_copied_without_following(self):
        target = self.root / "real.txt"
        target.write_text("keep me")
        link = self.root / "alias.txt"
        link.symlink_to(target)
        self.assertEqual(self.tools.path_info("alias.txt")["kind"], "symlink")
        listed = {entry["name"]: entry for entry in self.tools.list_dir(".")["entries"]}
        self.assertEqual(listed["alias.txt"]["path"], str(link))
        self.assertEqual(listed["alias.txt"]["kind"], "symlink")
        self.tools.file_op("copy", "alias.txt", "copy.txt")
        self.assertTrue((self.root / "copy.txt").is_symlink())
        self.tools.file_op("remove", "alias.txt")
        self.assertFalse(link.is_symlink())
        self.assertEqual(target.read_text(), "keep me")
        directory = self.root / "real_dir"
        directory.mkdir()
        (directory / "data").write_text("safe")
        dirlink = self.root / "alias_dir"
        dirlink.symlink_to(directory, target_is_directory=True)
        self.tools.file_op("copy", "alias_dir", "copy_dir_link")
        self.assertTrue((self.root / "copy_dir_link").is_symlink())
        self.tools.file_op("remove", "alias_dir", recursive=True)
        self.assertTrue((directory / "data").exists())
        self.assertFalse(dirlink.is_symlink())
        source = self.root / "source"
        source.mkdir()
        (source / "reference").symlink_to(target)
        self.tools.file_op("copy", "source", "new", recursive=True)
        self.assertTrue((self.root / "new" / "reference").is_symlink())

    def test_search_python_fallback_preserves_regex_and_column(self):
        (self.root / "a.txt").write_text("ábc needle\nhello world\n")
        with patch("host_sandbox.core.shutil.which", return_value=None):
            hits = self.tools.search_text("nee.le", ".", fixed_strings=False, glob=["*.txt"])
            self.assertEqual(hits["engine"], "python")
            self.assertEqual(len(hits["results"]), 1)
            self.assertIn(":1:6:ábc needle", hits["results"][0])
            structured = RouterMCP._structured_search(hits, session_id="s_1234567890123456", repo=".", query="nee.le", target=".")
            self.assertEqual(structured["match_count_returned"], 1)
            self.assertEqual(structured["matches"][0]["column"], 6)
            fixed = self.tools.search_text("needle", ".", fixed_strings=True, glob=["*.txt"])
            self.assertEqual(len(fixed["results"]), 1)
            literal = self.tools.search_text("nee.le", ".", fixed_strings=True, glob=["*.txt"])
            self.assertEqual(len(literal["results"]), 0)


if __name__ == "__main__":
    unittest.main()
