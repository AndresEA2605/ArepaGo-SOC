import unittest

from services.incident_simulator import benchmark_rows, reference_scenario, run_local_benchmark, simulate_mitigations
from services.metrics import RequestMetrics
from services.order_states import can_confirm_cash_payment, can_manage_payment, can_transition_order


class OrderTransitionTests(unittest.TestCase):
    def test_only_valid_forward_transitions_are_allowed(self):
        valid = [("placed", "confirmed"), ("confirmed", "preparing"),
                 ("preparing", "out_for_delivery"), ("out_for_delivery", "delivered")]
        for current, target in valid:
            with self.subTest(current=current, target=target):
                self.assertTrue(can_transition_order(current, target))
        self.assertTrue(can_transition_order("placed", "cancelled"))
        self.assertFalse(can_transition_order("delivered", "preparing"))
        self.assertFalse(can_transition_order("cancelled", "confirmed"))
        self.assertFalse(can_transition_order("placed", {"status": "confirmed"}))

    def test_cash_confirmation_requires_delivered_order_and_pending_cod_payment(self):
        self.assertTrue(can_confirm_cash_payment("cash_on_delivery", "pending", "delivered"))
        self.assertFalse(can_confirm_cash_payment("cash_on_delivery", "pending", "preparing"))
        self.assertFalse(can_confirm_cash_payment("cash_on_delivery", "paid", "delivered"))
        self.assertFalse(can_confirm_cash_payment("card", "pending", "delivered"))

    def test_customers_can_only_manage_their_own_payments(self):
        self.assertTrue(can_manage_payment("customer", "user-1", "user-1"))
        self.assertFalse(can_manage_payment("customer", "user-1", "user-2"))
        self.assertFalse(can_manage_payment("customer", "", ""))
        self.assertTrue(can_manage_payment("admin", "admin-1", "user-2"))
        self.assertFalse(can_manage_payment("root", "admin-1", "admin-1"))


class RequestMetricsTests(unittest.TestCase):
    def test_counts_5xx_and_calculates_p95_from_observed_requests(self):
        metrics = RequestMetrics()
        metrics.observe("pedidos", 12, 201, "GET", "/orders")
        metrics.observe("pedidos", 220, 503, "GET", "/orders/123e4567-e89b-12d3-a456-426614174000")
        metrics.observe("catalogo", 7, 200)
        metrics.observe("unknown", 999, 500)
        snapshot = metrics.snapshot()["services"]
        self.assertEqual(snapshot["pedidos"]["request_count"], 2)
        self.assertEqual(snapshot["pedidos"]["error_count"], 1)
        self.assertEqual(snapshot["pedidos"]["latency_p95_ms"], 220)
        self.assertEqual(snapshot["catalogo"]["request_count"], 1)
        self.assertEqual(snapshot["pagos"]["request_count"], 0)
        routes = snapshot["pedidos"]["route_diagnostics"]
        self.assertEqual(routes[0]["route"], "/orders/{id}")
        self.assertEqual(routes[0]["statuses"], {"503": 1})


class IncidentSimulationTests(unittest.TestCase):
    def test_reference_is_labeled_and_matches_reported_baseline(self):
        scenario = reference_scenario()
        self.assertTrue(scenario["simulation_only"])
        self.assertFalse(scenario["persisted"])
        self.assertEqual(scenario["date"], "2026-09-27")
        self.assertEqual(scenario["report"]["orders_examined"], 14_200_000)
        self.assertEqual(scenario["connection_pools"]["pedidos"]["configured"], 20)
        self.assertEqual(scenario["modules"]["pedidos"]["failure_rate_percent"], 64)
        self.assertEqual(scenario["modules"]["pagos"]["failure_rate_percent"], 59)
        self.assertEqual(scenario["economic_impact_cop"], 5_182_760_000.0)
        self.assertEqual(scenario["affected_customers"], 103_800)
        self.assertEqual(scenario["social_media_mentions"], 34_200)
        self.assertEqual(scenario["peak_window"]["sample_requests"], 600)
        self.assertEqual(scenario["peak_window"]["pedidos_failed"], 384)
        self.assertEqual(scenario["peak_window"]["pagos_failed"], 354)

    def test_all_three_mitigations_are_separate_qualitative_comparisons(self):
        result = simulate_mitigations({"mitigations": ["reschedule", "index", "read_replica"]})
        self.assertFalse(result["persisted"])
        self.assertEqual({item["id"] for item in result["mitigations"]},
                         {"reschedule", "index", "read_replica"})
        reschedule = next(item for item in result["mitigations"] if item["id"] == "reschedule")
        self.assertFalse(reschedule["after"]["business_window_overlap"])
        self.assertIn("EXPLAIN ANALYZE", next(item for item in result["mitigations"] if item["id"] == "index")["after"]["query_plan"])

    def test_mitigation_input_is_validated(self):
        for value in (None, [], ["unknown"], [{}], ["index"] * 4):
            with self.subTest(value=value), self.assertRaises(ValueError):
                simulate_mitigations({"mitigations": value})

    def test_local_benchmark_size_is_bounded(self):
        self.assertEqual(benchmark_rows(100_000), 100_000)
        for value in (999, 500_001, True, "1000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                benchmark_rows(value)

    def test_benchmark_uses_temporary_table_and_only_adds_index_when_selected(self):
        class FakeDB:
            def __init__(self): self.sql = []; self.explains = 0
            def execute(self, sql, params=()):
                self.sql.append(sql)
                if sql.startswith("EXPLAIN"):
                    self.explains += 1
                return self
            def fetchone(self):
                node = "Seq Scan" if self.explains == 1 else "Index Only Scan"
                return {"QUERY PLAN": [{"Execution Time": 1.0 if self.explains == 1 else 0.5,
                    "Planning Time": 0.1, "Plan": {"Node Type": node, "Shared Hit Blocks": 1,
                    "Shared Read Blocks": 0}}]}
        db = FakeDB()
        measured = run_local_benchmark(db, 1000, apply_index=True)
        self.assertTrue(any("CREATE TEMP TABLE" in sql and "ON COMMIT DROP" in sql for sql in db.sql))
        self.assertTrue(any("CREATE INDEX" in sql for sql in db.sql))
        self.assertEqual(measured["before_index"]["plan_nodes"], ["Seq Scan"])
        self.assertEqual(measured["after_index"]["plan_nodes"], ["Index Only Scan"])


if __name__ == "__main__":
    unittest.main()
