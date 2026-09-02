import unittest

from host_sandbox.agent_client import _payload_expired


class AgentClientTests(unittest.TestCase):
    def test_payload_expiry(self):
        self.assertFalse(_payload_expired({"call_id": "c"}, now=100.0))
        self.assertFalse(_payload_expired({"expires_at": 101.0}, now=100.0))
        self.assertTrue(_payload_expired({"expires_at": 100.0}, now=100.0))
        self.assertTrue(_payload_expired({"expires_at": 99.0}, now=100.0))


if __name__ == "__main__":
    unittest.main()
