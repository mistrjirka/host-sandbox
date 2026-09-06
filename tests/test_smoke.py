import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from host_sandbox.audit import AuditLog
from host_sandbox.core import HostTools, TERMINAL_JOB_STATES
from host_sandbox.mcp import MCPServer


class Smoke(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name); self.audit = AuditLog(root/"state"); self.tools = HostTools(self.audit, root/"state", str(root)); self.mcp = MCPServer(self.tools, self.audit)
    def tearDown(self): self.tools.close(); self.tmp.cleanup()
    def call(self, name, args):
        r=self.mcp.handle({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":name,"arguments":args}}); self.assertIsNotNone(r); return r["result"]["structuredContent"]
    def test_files_and_binary_transfer(self):
        self.call("write_file", {"path":"a.txt","content":"hello"}); self.assertEqual(self.call("read_file", {"path":"a.txt"})["text"],"hello")
        chunk=self.call("read_file_chunk", {"path":"a.txt"}); self.call("write_file_chunk", {"path":"b.bin","data_base64":chunk["data_base64"],"truncate_after":True}); self.assertEqual((Path(self.tmp.name)/"b.bin").read_bytes(),b"hello")
    def test_exec_and_job(self):
        r=self.call("exec_command", {"command":"printf abc","wait_seconds":2}); self.assertEqual(r["exit_code"],0); self.assertIn("abc",r["output_tail"])
        r=self.call("exec_command", {"command":"sleep .2; printf done","wait_seconds":0})
        deadline=time.time()+2
        rr=self.call("read_job", {"job_id":r["id"]})
        while rr["status"] not in {"finished","failed","timed_out","cancelled","interrupted"} and time.time()<deadline:
            time.sleep(.05); rr=self.call("read_job", {"job_id":r["id"]})
        self.assertEqual(rr["exit_code"],0); self.assertIn("done",rr["output"])
    def test_exec_preserves_inherited_path_and_user_shell(self):
        with patch.dict(os.environ, {"PATH":"/sentinel:/usr/bin:/bin", "SHELL":"/bin/sh"}, clear=False):
            r=self.call("exec_command", {"command":"printf '%s|%s' \"$PATH\" \"$0\"", "wait_seconds":2})
        self.assertEqual(r["exit_code"], 0)
        self.assertTrue(r["output_tail"].startswith("/sentinel:/usr/bin:/bin|"), r["output_tail"])
        self.assertIn("/bin/sh", r["output_tail"])

    @unittest.skipUnless(os.name == "posix" and shutil.which("setsid") and Path("/proc").is_dir(), "requires Linux /proc and setsid")
    def test_timeout_reaps_detached_descendants(self):
        dollar = "$"
        command = f"setsid sh -c 'echo {dollar}{dollar} > escaped.pid; sleep 60' & sleep 60"
        result = self.tools.exec_command(command, wait_seconds=0, timeout_seconds=1)
        pid_file = self.root / "escaped.pid" if hasattr(self, "root") else Path(self.tmp.name) / "escaped.pid"
        deadline = time.time() + 2
        while not pid_file.exists() and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(pid_file.exists())
        escaped_pid = int(pid_file.read_text().strip())
        deadline = time.time() + 3
        alive = True
        while time.time() < deadline:
            try:
                state = Path(f"/proc/{escaped_pid}/stat").read_text().split()[2]
                alive = state != "Z"
            except FileNotFoundError:
                alive = False
            if not alive:
                break
            time.sleep(0.05)
        deadline = time.time() + 2
        job = self.tools.read_job(result["id"], max_bytes=1000)
        while job["status"] not in {"finished", "failed", "timed_out", "cancelled", "interrupted"} and time.time() < deadline:
            time.sleep(0.05)
            job = self.tools.read_job(result["id"], max_bytes=1000)
        self.assertEqual(job["status"], "timed_out")
        self.assertEqual(job["timeout_stage"], "execution")
        self.assertFalse(alive, f"detached PID {escaped_pid} survived job timeout")

    @unittest.skipUnless(os.name == "posix", "resource locks currently require POSIX fcntl")
    def test_resource_lock_covers_full_durable_job_and_has_independent_wait_timeout(self):
        root = Path(self.tmp.name)
        first = self.tools.exec_command(
            "date +%s%N > a.start; sleep 1; date +%s%N > a.end",
            wait_seconds=0,
            timeout_seconds=5,
            resource_locks=["gpu:all"],
            resource_lock_wait_seconds=5,
        )
        time.sleep(0.1)
        second = self.tools.exec_command(
            "date +%s%N > b.start; sleep .1; date +%s%N > b.end",
            wait_seconds=0,
            timeout_seconds=5,
            resource_locks=["gpu:all"],
            resource_lock_wait_seconds=5,
        )
        time.sleep(0.1)
        self.assertEqual(self.tools.read_job(second["id"], max_bytes=1000)["status"], "waiting_for_lock")
        rejected = self.tools.exec_command(
            "echo SHOULD_NOT_RUN > rejected.ran",
            wait_seconds=1,
            timeout_seconds=5,
            resource_locks=["gpu:all"],
            resource_lock_wait_seconds=0,
        )
        self.assertEqual(rejected["status"], "timed_out")
        self.assertEqual(rejected["timeout_stage"], "resource_lock")
        self.assertFalse((root / "rejected.ran").exists())
        deadline = time.time() + 4
        while time.time() < deadline:
            if self.tools.read_job(second["id"], max_bytes=1000)["status"] in {"finished", "failed", "timed_out"}:
                break
            time.sleep(0.05)
        self.assertGreaterEqual(int((root / "b.start").read_text()), int((root / "a.end").read_text()))
        self.assertEqual(self.tools.read_job(first["id"], max_bytes=1000)["status"], "finished")
        self.assertEqual(self.tools.read_job(second["id"], max_bytes=1000)["status"], "finished")

    def test_persisted_running_job_can_be_recovered_and_cancelled(self):
        root = Path(self.tmp.name)
        recovered_tools = None
        result = self.tools.exec_command("sleep 3; printf recovered", wait_seconds=0, timeout_seconds=10)
        time.sleep(0.2)
        try:
            recovered_tools = HostTools(AuditLog(root / "state"), root / "state", str(root))
            recovered = recovered_tools.read_job(result["id"], max_bytes=1000)
            self.assertEqual(recovered["id"], result["id"])
            self.assertIn(recovered["status"], {"running", "waiting_for_lock"})
            stopped = recovered_tools.signal_job(result["id"], "TERM", 1)
            self.assertEqual(stopped["status"], "cancelled")
        finally:
            if recovered_tools is not None:
                recovered_tools.close()

    @unittest.skipUnless(os.name == "posix", "process association requires POSIX signals")
    def test_signal_process_on_job_main_pid_cancels_whole_job(self):
        result = self.tools.exec_command("sleep 60 & sleep 60", wait_seconds=0, timeout_seconds=120)
        deadline = time.time() + 2
        job = self.tools.read_job(result["id"], max_bytes=1000)
        while not job.get("pid") and time.time() < deadline:
            time.sleep(0.05)
            job = self.tools.read_job(result["id"], max_bytes=1000)
        self.assertIsNotNone(job.get("pid"))
        signalled = self.tools.signal_process(int(job["pid"]), "TERM")
        self.assertEqual(signalled["associated_job_id"], result["id"])
        deadline = time.time() + 2
        while self.tools.read_job(result["id"], max_bytes=1000)["status"] not in TERMINAL_JOB_STATES and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.tools.read_job(result["id"], max_bytes=1000)["status"], "cancelled")

    def test_initialize_and_list(self):
        r=self.mcp.handle({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}); self.assertIn("protocolVersion",r["result"])
        r=self.mcp.handle({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}); names={t["name"] for t in r["result"]["tools"]}; self.assertIn("exec_command",names); self.assertIn("write_file_chunk",names)
        exec_tool=next(t for t in r["result"]["tools"] if t["name"]=="exec_command")
        props=exec_tool["inputSchema"]["properties"]
        self.assertEqual(props["resource_locks"]["maxItems"],16)
        self.assertEqual(props["resource_lock_wait_seconds"]["maximum"],604800)

if __name__ == "__main__": unittest.main()
