import unittest

from services.rate_limit import SlidingWindowLimiter, resolve_client_ip


class ClientIpTests(unittest.TestCase):
    def test_prefers_real_ip_header_set_by_the_edge_proxy(self):
        self.assertEqual(resolve_client_ip("203.0.113.7", "198.51.100.9", "172.18.0.5"),
                         "203.0.113.7")

    def test_uses_last_forwarded_hop_instead_of_the_spoofable_first_one(self):
        self.assertEqual(resolve_client_ip(None, "1.2.3.4, 198.51.100.9", "172.18.0.5"),
                         "198.51.100.9")

    def test_ignores_invalid_values_and_falls_back_to_the_socket_peer(self):
        self.assertEqual(resolve_client_ip("not-an-ip", "also-bad", "172.18.0.5"),
                         "172.18.0.5")

    def test_returns_unknown_when_nothing_parses(self):
        self.assertEqual(resolve_client_ip(None, None, None), "unknown")

    def test_normalizes_ipv6(self):
        self.assertEqual(resolve_client_ip("2001:0db8::1", None, None), "2001:db8::1")


class SlidingWindowLimiterTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.limiter = SlidingWindowLimiter(clock=lambda: self.now)

    def test_blocks_after_limit_and_recovers_after_window(self):
        for _ in range(3):
            self.assertTrue(self.limiter.allow("ip|user", 3, 60))
        self.assertFalse(self.limiter.allow("ip|user", 3, 60))
        self.now = 60.0
        self.assertTrue(self.limiter.allow("ip|user", 3, 60))

    def test_keys_are_independent(self):
        self.assertTrue(self.limiter.allow("a", 1, 60))
        self.assertTrue(self.limiter.allow("b", 1, 60))
        self.assertFalse(self.limiter.allow("a", 1, 60))

    def test_reset_clears_attempts(self):
        self.limiter.allow("a", 1, 60)
        self.limiter.reset("a")
        self.assertTrue(self.limiter.allow("a", 1, 60))

    def test_expired_keys_are_pruned_when_the_key_cap_is_exceeded(self):
        limiter = SlidingWindowLimiter(max_keys=1, clock=lambda: self.now)
        limiter.allow("old", 1, 60)
        self.now = 61.0
        limiter.allow("new", 1, 60)
        self.now = 62.0
        limiter.allow("other", 1, 60)
        self.assertNotIn("old", limiter._attempts)


if __name__ == "__main__":
    unittest.main()
