import os
import platform
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from host_sandbox.cli import client_agent_token, parser


class CLITests(unittest.TestCase):
    def test_connect_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            args = parser().parse_args(["connect"])
        self.assertEqual(args.hub, "http://10.8.0.9:8767")
        self.assertEqual(args.name, platform.node() or "computer")

    def test_client_token_file(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HOME": td}, clear=True):
            p = Path(td) / ".config/host-sandbox/client.env"
            p.parent.mkdir(parents=True)
            p.write_text("HOST_SANDBOX_AGENT_TOKEN=test-secret\n")
            # Path.home() can cache platform home semantics, so patch the helper's returned path.
            with patch("host_sandbox.cli.default_client_env", return_value=p):
                self.assertEqual(client_agent_token(None), "test-secret")


if __name__ == "__main__":
    unittest.main()
