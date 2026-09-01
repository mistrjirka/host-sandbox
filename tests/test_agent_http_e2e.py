import base64
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from host_sandbox.agent_server import AgentHTTPServer, run_agent_http
from host_sandbox.agent_transport import AgentRegistry
from host_sandbox.router_mcp import RouterMCP
from host_sandbox.ssh_router import SSHRouter


class AgentHTTPE2E(unittest.TestCase):
    def test_native_binary_image_and_job_streams_over_foreground_http_agent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "hosts.json"
            config.write_text('{"hosts":[{"name":"hub","local":true}]}')
            binary = bytes(range(256)) * 8192  # 2 MiB, verifies the old 1 MiB ceiling is gone.
            (root / "large.bin").write_bytes(binary)
            png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WlWlZsAAAAASUVORK5CYII=")
            (root / "tiny.png").write_bytes(png)

            registry = AgentRegistry(lease_seconds=15)
            router = SSHRouter(config, registry)
            aggregate = RouterMCP(router, root / "sessions.json")
            server = AgentHTTPServer(("127.0.0.1", 0), registry, "secret")
            thread = threading.Thread(target=run_agent_http, args=(server,), daemon=True)
            thread.start()
            port = server.server_address[1]

            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            process = subprocess.Popen(
                [
                    sys.executable, "-m", "host_sandbox.cli", "--name", "homer", "--cwd", str(root),
                    "--state-dir", str(root / "client-state"), "connect", "--hub", f"http://127.0.0.1:{port}",
                    "--token", "secret", "--no-dashboard",
                ],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            try:
                deadline = time.time() + 8
                while time.time() < deadline and not registry.is_online("homer"):
                    time.sleep(0.05)
                self.assertTrue(registry.is_online("homer"))
                sid = aggregate.call("create_session", {"project": "homer"})["id"]

                binary_result = aggregate.handle({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "read_binary_file", "arguments": {"session_id": sid, "path": "large.bin"}},
                })["result"]
                resource = binary_result["content"][0]["resource"]
                self.assertEqual(len(base64.b64decode(resource["blob"])), len(binary))
                self.assertNotIn("content_base64", binary_result["structuredContent"])

                image_result = aggregate.handle({
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "view_image", "arguments": {"session_id": sid, "path": "tiny.png"}},
                })["result"]
                self.assertEqual(image_result["content"][0]["type"], "image")
                self.assertEqual(image_result["content"][0]["mimeType"], "image/png")

                job = aggregate.call("exec_command", {
                    "session_id": sid, "command": "printf foreground-out; printf foreground-err >&2", "wait_seconds": 2,
                })
                self.assertEqual(job["stdout"], "foreground-out")
                self.assertEqual(job["stderr"], "foreground-err")
            finally:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=2)
                if process.stdout is not None:
                    process.stdout.close()
                server.shutdown(); server.server_close(); router.close()


if __name__ == "__main__":
    unittest.main()
