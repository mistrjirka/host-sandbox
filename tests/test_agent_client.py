import unittest

from host_sandbox.agent_client import HubHTTPError, _payload_expired, poll_error_requires_reregistration, poll_retry_delay


class AgentClientTests(unittest.TestCase):
    def test_payload_expiry(self):
        self.assertFalse(_payload_expired({"call_id": "c"}, now=100.0))
        self.assertFalse(_payload_expired({"expires_at": 101.0}, now=100.0))
        self.assertTrue(_payload_expired({"expires_at": 100.0}, now=100.0))
        self.assertTrue(_payload_expired({"expires_at": 99.0}, now=100.0))

    def test_only_unknown_agent_http_error_requires_reregistration(self):
        self.assertTrue(poll_error_requires_reregistration(HubHTTPError(404, "unknown agent_id")))
        self.assertFalse(poll_error_requires_reregistration(HubHTTPError(500, "temporary")))
        self.assertFalse(poll_error_requires_reregistration(TimeoutError("timed out")))

    def test_poll_timeout_retries_quickly_without_long_backoff(self):
        self.assertEqual(poll_retry_delay(TimeoutError("timed out")), 0.25)
        self.assertEqual(poll_retry_delay(RuntimeError("other")), 1.0)


if __name__ == "__main__":
    unittest.main()
