import base64
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from host_sandbox.audit import AuditLog
from host_sandbox.core import HostTools
from host_sandbox.mcp import MCPServer
from host_sandbox.router_mcp import ROUTER_TOOLS, RouterMCP
from host_sandbox.ssh_router import HostConfig


class InProcessRouter:
    def __init__(self, mcp: MCPServer):
        self.mcp = mcp
        self.hosts = {"pc": HostConfig("pc", local=True)}

    def has_host(self, name):
        return name == "pc"

    def project_entries(self):
        return [{"id": "pc", "name": "pc", "description": "test host", "transport": "in-process"}]

    def list_hosts(self):
        return {"hosts": [{"name": "pc", "online": True, "transport": "in-process"}]}

    def call_tool(self, host, name, arguments):
        if host != "pc":
            raise KeyError(host)
        response = self.mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        if response is None:
            raise RuntimeError("missing MCP response")
        return response["result"]

    def close(self):
        pass


class ParityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audit = AuditLog(self.root / "state")
        self.tools = HostTools(self.audit, self.root / "state", str(self.root))
        self.local_mcp = MCPServer(self.tools, self.audit)
        self.router = InProcessRouter(self.local_mcp)
        self.aggregate = RouterMCP(self.router, self.root / "sessions.json")
        self.sid = self.aggregate.call("create_session", {"project": "pc"})["id"]

    def tearDown(self):
        self.tools.close()
        self.tmp.cleanup()

    def test_public_surface_matches_current_development_sandbox(self):
        expected = {
            "sandbox_health", "list_projects", "list_sessions", "create_session", "list_repositories", "path_info",
            "exec_command", "exec_commands", "list_jobs", "get_job", "delete_job", "cleanup_jobs", "terminate_job",
            "search_project", "search_many", "read_file", "read_files", "read_binary_file", "view_image",
            "write_binary_file", "write_file", "replace_text", "apply_patch", "git_status_diff", "list_processes",
            "signal_process", "read_terminal", "send_terminal_input", "send_terminal_key", "destroy_session",
        }
        self.assertEqual({tool["name"] for tool in ROUTER_TOOLS}, expected)
        self.assertEqual(len(ROUTER_TOOLS), 30)

    def test_separate_stdout_stderr_jobs_delete_and_cleanup(self):
        result = self.aggregate.call("exec_command", {
            "session_id": self.sid,
            "command": "printf stdout-abc; printf stderr-xyz >&2",
            "wait_seconds": 2,
        })
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "stdout-abc")
        self.assertEqual(result["stderr"], "stderr-xyz")
        read = self.aggregate.call("get_job", {
            "session_id": self.sid, "job_id": result["id"], "stdout_offset": 3, "stderr_offset": 3, "max_bytes": 10000,
        })
        self.assertEqual(read["stdout"], "out-abc")
        self.assertEqual(read["stderr"], "err-xyz")
        deleted = self.aggregate.call("delete_job", {"session_id": self.sid, "job_id": result["id"]})
        self.assertTrue(deleted["deleted"])
        self.aggregate.call("exec_command", {"session_id": self.sid, "command": "true", "wait_seconds": 2})
        preview = self.aggregate.call("cleanup_jobs", {
            "session_id": self.sid, "older_than_seconds": 0, "keep_recent_per_session": 0, "dry_run": True,
        })
        self.assertGreaterEqual(preview["candidate_count"], 1)
        cleaned = self.aggregate.call("cleanup_jobs", {
            "session_id": self.sid, "older_than_seconds": 0, "keep_recent_per_session": 0, "dry_run": False,
        })
        self.assertGreaterEqual(cleaned["deleted_count"], 1)

    def test_read_files_and_search_many(self):
        (self.root / "a.txt").write_text("alpha\nneedle one\nomega\n")
        (self.root / "b.txt").write_text("beta\nneedle two\n")
        reads = self.aggregate.call("read_files", {
            "files": [
                {"session_id": self.sid, "path": "a.txt", "start_line": 2, "max_lines": 1},
                {"session_id": self.sid, "path": "b.txt", "start_line": 1, "max_lines": 2},
            ], "concurrency": 2,
        })
        self.assertEqual(reads["count"], 2)
        self.assertIn("2: needle one", reads["results"][0]["result"]["content"])
        searches = self.aggregate.call("search_many", {
            "searches": [
                {"session_id": self.sid, "query": "needle", "path": ".", "glob": ["*.txt"], "fixed_strings": True},
                {"session_id": self.sid, "query": "omega", "path": "a.txt", "fixed_strings": True},
            ], "concurrency": 2,
        })
        self.assertEqual(searches["count"], 2)
        self.assertEqual(searches["results"][0]["result"]["match_count_returned"], 2)
        match = searches["results"][0]["result"]["matches"][0]
        self.assertIn("path", match); self.assertIn("line", match); self.assertIn("column", match); self.assertIn("text", match)

    def test_native_binary_and_image_content_and_binary_write(self):
        binary = b"\x00\x01abc\xff"
        (self.root / "blob.bin").write_bytes(binary)
        response = self.aggregate.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_binary_file", "arguments": {"session_id": self.sid, "path": "blob.bin"}}})
        body = response["result"]
        self.assertEqual(body["content"][0]["type"], "resource")
        resource = body["content"][0]["resource"]
        self.assertEqual(base64.b64decode(resource["blob"]), binary)
        self.assertEqual(resource["mimeType"], "application/octet-stream")
        self.assertNotIn("content_base64", body["structuredContent"])
        self.assertTrue(resource["uri"].startswith("host-sandbox://"))

        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WlWlZsAAAAASUVORK5CYII=")
        (self.root / "tiny.png").write_bytes(png)
        image_response = self.aggregate.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "view_image", "arguments": {"session_id": self.sid, "path": "tiny.png"}}})["result"]
        image = image_response["content"][0]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(image["data"]), png)
        self.assertNotIn("content_base64", image_response["structuredContent"])

        target = b"written-binary\x00\xff"
        written = self.aggregate.call("write_binary_file", {
            "session_id": self.sid, "path": "written.bin", "content_base64": base64.b64encode(target).decode(),
        })
        self.assertEqual((self.root / "written.bin").read_bytes(), target)
        self.assertEqual(written["bytes"], len(target))

    def test_git_patch_and_status_diff(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
        (repo / "x.txt").write_text("old\n")
        subprocess.run(["git", "-C", str(repo), "add", "x.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        patch = "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n@@ -1 +1 @@\n-old\n+new\n"
        checked = self.aggregate.call("apply_patch", {"session_id": self.sid, "repo": "repo", "patch": patch, "check_only": True})
        self.assertFalse(checked["applied"])
        applied = self.aggregate.call("apply_patch", {"session_id": self.sid, "repo": "repo", "patch": patch})
        self.assertTrue(applied["applied"])
        self.assertEqual((repo / "x.txt").read_text(), "new\n")
        status = self.aggregate.call("git_status_diff", {"session_id": self.sid, "repo": "repo", "max_total_chars": 10000})
        self.assertIn("x.txt", status["status"])
        self.assertIn("-old", status["diff"])
        self.assertEqual(status["repo"], "repo")
        self.assertEqual(status["repository_root"], str(repo))

    @unittest.skipUnless(shutil.which("tmux"), "tmux unavailable")
    def test_interactive_terminal(self):
        terminal_name = self.tools._terminal_name(self.sid)
        try:
            first = self.aggregate.call("read_terminal", {"session_id": self.sid, "screen_lines": 20})
            self.assertTrue(first["running"])
            sent = self.aggregate.call("send_terminal_input", {"session_id": self.sid, "text": "printf 'terminal-ok\\n'", "press_enter": True})
            self.assertTrue(sent["sent"])
            time.sleep(0.25)
            read = self.aggregate.call("read_terminal", {"session_id": self.sid, "cursor": 0, "screen_lines": 30})
            self.assertIn("terminal-ok", read["screen"] + read["output"])
            keyed = self.aggregate.call("send_terminal_key", {"session_id": self.sid, "key": "Enter"})
            self.assertTrue(keyed["sent"])
        finally:
            subprocess.run(["tmux", "kill-session", "-t", terminal_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
