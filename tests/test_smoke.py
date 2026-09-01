import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from host_sandbox.audit import AuditLog
from host_sandbox.core import HostTools
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
        r=self.call("exec_command", {"command":"sleep .2; printf done","wait_seconds":0}); time.sleep(.35); rr=self.call("read_job", {"job_id":r["id"]}); self.assertEqual(rr["exit_code"],0); self.assertIn("done",rr["output"])
    def test_exec_preserves_inherited_path_and_user_shell(self):
        with patch.dict(os.environ, {"PATH":"/sentinel:/usr/bin:/bin", "SHELL":"/bin/sh"}, clear=False):
            r=self.call("exec_command", {"command":"printf '%s|%s' \"$PATH\" \"$0\"", "wait_seconds":2})
        self.assertEqual(r["exit_code"], 0)
        self.assertTrue(r["output_tail"].startswith("/sentinel:/usr/bin:/bin|"), r["output_tail"])
        self.assertIn("/bin/sh", r["output_tail"])

    def test_initialize_and_list(self):
        r=self.mcp.handle({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}); self.assertIn("protocolVersion",r["result"])
        r=self.mcp.handle({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}); names={t["name"] for t in r["result"]["tools"]}; self.assertIn("exec_command",names); self.assertIn("write_file_chunk",names)

if __name__ == "__main__": unittest.main()
