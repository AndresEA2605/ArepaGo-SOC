import os
import unittest
from unittest import mock

from services.gateway import origin_is_allowed, validate_runtime_config
from services.gateway import metrics_payload


class OriginTests(unittest.TestCase):
    def test_accepts_same_origin_browser_request(self):
        self.assertTrue(origin_is_allowed("https://app.example.com", "app.example.com", "https", True))

    def test_rejects_cross_site_browser_request(self):
        self.assertFalse(origin_is_allowed("https://evil.example", "app.example.com", "https", True))

    def test_rejects_scheme_mismatch(self):
        self.assertFalse(origin_is_allowed("http://app.example.com", "app.example.com", "https", True))

    def test_allows_api_clients_without_cookie(self):
        self.assertTrue(origin_is_allowed(None, "app.example.com", "https", False))

    def test_rejects_cookie_request_without_origin(self):
        self.assertFalse(origin_is_allowed(None, "app.example.com", "https", True))


class MetricsPayloadTests(unittest.TestCase):
    def test_metrics_payload_wraps_the_process_list(self):
        payload = metrics_payload()
        self.assertEqual(payload["source"], "transaction-router process memory")
        self.assertIsInstance(payload["services"], list)


class ProductionConfigTests(unittest.TestCase):
    def test_rejects_insecure_cookie_in_production(self):
        with mock.patch.dict(os.environ, {
            "APP_ENV": "production",
            "AUTH_COOKIE_SECURE": "false",
            "INTERNAL_AUTH_SECRET_FILE": "/run/secrets/internal_auth_secret",
        }, clear=False):
            with self.assertRaises(RuntimeError):
                validate_runtime_config()

    def test_rejects_missing_internal_secret_in_production(self):
        with mock.patch.dict(os.environ, {
            "APP_ENV": "production",
            "AUTH_COOKIE_SECURE": "true",
        }, clear=False):
            os.environ.pop("INTERNAL_AUTH_SECRET_FILE", None)
            os.environ.pop("INTERNAL_AUTH_SECRET", None)
            with self.assertRaises(RuntimeError):
                validate_runtime_config()

    def test_accepts_hardened_production_config(self):
        with mock.patch.dict(os.environ, {
            "APP_ENV": "production",
            "AUTH_COOKIE_SECURE": "true",
            "INTERNAL_AUTH_SECRET_FILE": "/run/secrets/internal_auth_secret",
        }, clear=False):
            validate_runtime_config()

    def test_rejects_invalid_upstream_timeout(self):
        with mock.patch("services.gateway.TIMEOUT_SECONDS", 0):
            with self.assertRaises(RuntimeError):
                validate_runtime_config()


if __name__ == "__main__":
    unittest.main()
