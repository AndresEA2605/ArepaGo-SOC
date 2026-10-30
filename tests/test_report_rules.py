import unittest

from services.report_rules import can_transition_report, validate_report


class ReportRulesTests(unittest.TestCase):
    def test_accepts_valid_store_and_website_reports(self):
        for category in ("web_issue", "store_complaint"):
            result = validate_report({"category": category, "subject": "No carga el catálogo",
                                      "message": "La página del catálogo queda cargando."})
            self.assertEqual(result["category"], category)
            self.assertIsNone(result["order_id"])

    def test_rejects_invalid_category_text_and_order_id(self):
        base = {"category": "web_issue", "subject": "No carga el catálogo",
                "message": "La página del catálogo queda cargando."}
        for changes in ({"category": "other"}, {"subject": "x"}, {"message": "corto"},
                        {"order_id": "no-es-uuid"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_report({**base, **changes})

    def test_report_status_transitions(self):
        self.assertTrue(can_transition_report("new", "in_review"))
        self.assertTrue(can_transition_report("in_review", "resolved"))
        self.assertTrue(can_transition_report("closed", "in_review"))
        self.assertFalse(can_transition_report("new", "unknown"))
        self.assertFalse(can_transition_report("resolved", "new"))


if __name__ == "__main__":
    unittest.main()
