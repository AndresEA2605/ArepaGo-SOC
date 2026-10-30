import unittest

from services.measurement_aggregate import aggregate_measurements, merge_diagnostics


def measurement(replica, requests, errors, latency, pool, diagnostics, timestamp="2026-10-30T20:00:00+00:00", mid="a"):
    return {
        "id": mid, "service": "pedidos", "timestamp": timestamp, "environment": "local",
        "replica": replica, "request_count": requests, "error_count": errors,
        "latency_p95_ms": latency, "db_pool_active": pool[0] if pool else None,
        "db_pool_max": pool[1] if pool else None, "diagnostics": diagnostics,
    }


class AggregateMeasurementsTest(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(aggregate_measurements([]))

    def test_sums_traffic_and_keeps_worst_replica(self):
        rows = [
            measurement("router-a", 10, 1, 120.5, (2, 10),
                        [{"method": "GET", "route": "/orders", "requests": 10, "errors": 1,
                          "statuses": {"500": 1, "200": 9}}], mid="a"),
            measurement("router-b", 15, 4, 2400.0, (8, 10),
                        [{"method": "GET", "route": "/orders", "requests": 15, "errors": 4,
                          "statuses": {"502": 4, "200": 11}}], mid="b"),
        ]
        result = aggregate_measurements(rows)
        self.assertEqual(result["request_count"], 25)
        self.assertEqual(result["error_count"], 5)
        self.assertEqual(result["latency_p95_ms"], 2400.0)
        self.assertEqual(result["db_pool_active"], 8)
        self.assertEqual(result["db_pool_max"], 10)
        self.assertEqual(result["replica_count"], 2)
        self.assertEqual(result["replicas"], ["router-a", "router-b"])
        self.assertEqual(result["sample_key"], "a|b")
        self.assertEqual(len(result["diagnostics"]), 1)
        self.assertEqual(result["diagnostics"][0]["requests"], 25)
        self.assertEqual(result["diagnostics"][0]["errors"], 5)
        self.assertEqual(result["diagnostics"][0]["statuses"], {"500": 1, "200": 20, "502": 4})

    def test_missing_pool_and_legacy_timestamp(self):
        rows = [measurement("router-a", 3, 0, 50.0, None, [],
                            timestamp="2026-10-30T20:00:00Z", mid="only")]
        result = aggregate_measurements(rows)
        self.assertIsNone(result["db_pool_active"])
        self.assertIsNone(result["db_pool_max"])
        self.assertEqual(result["timestamp"], "2026-10-30T20:00:00+00:00")
        self.assertEqual(result["replica_count"], 1)

    def test_merge_diagnostics_caps_and_sorts_by_errors(self):
        diagnostics = [{"method": "GET", "route": f"/route-{index}", "requests": 10,
                        "errors": index, "statuses": {"500": index}} for index in range(30)]
        merged = merge_diagnostics(diagnostics)
        self.assertEqual(len(merged), 20)
        self.assertEqual(merged[0]["route"], "/route-29")
        self.assertEqual(merged[-1]["route"], "/route-10")


if __name__ == "__main__":
    unittest.main()
