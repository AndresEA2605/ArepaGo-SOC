import unittest

from services.incident_rules import recovery_decision


class RecoveryDecisionTests(unittest.TestCase):
    def test_traffic_without_breach_increments_the_counter(self):
        action, count, reason = recovery_decision(
            requests=4, recovery_count=0, recovery_required=3,
            seconds_since_breach=15, inactivity_seconds=1800)
        self.assertEqual((action, count, reason), ("recovering", 1, None))

    def test_required_consecutive_samples_resolve_the_incident(self):
        action, count, reason = recovery_decision(
            requests=4, recovery_count=2, recovery_required=3,
            seconds_since_breach=15, inactivity_seconds=1800)
        self.assertEqual((action, count, reason), ("resolved", 3, "recovered_with_traffic"))

    def test_quiet_window_neither_increments_nor_resets(self):
        action, count, reason = recovery_decision(
            requests=0, recovery_count=2, recovery_required=3,
            seconds_since_breach=60, inactivity_seconds=1800)
        self.assertEqual((action, count, reason), ("awaiting_traffic", 2, None))

    def test_inactivity_closes_a_stale_incident(self):
        action, count, reason = recovery_decision(
            requests=0, recovery_count=1, recovery_required=3,
            seconds_since_breach=1800, inactivity_seconds=1800)
        self.assertEqual((action, count, reason), ("resolved_inactive", 1, "inactivity_timeout"))

    def test_inactivity_can_be_disabled(self):
        action, _, reason = recovery_decision(
            requests=0, recovery_count=0, recovery_required=3,
            seconds_since_breach=10**9, inactivity_seconds=0)
        self.assertEqual((action, reason), ("awaiting_traffic", None))

    def test_rejects_invalid_recovery_requirement(self):
        with self.assertRaises(ValueError):
            recovery_decision(1, 0, 1, 0, 1800)

    def test_rejects_negative_elapsed_time(self):
        with self.assertRaises(ValueError):
            recovery_decision(1, 0, 3, -1, 1800)


if __name__ == "__main__":
    unittest.main()
