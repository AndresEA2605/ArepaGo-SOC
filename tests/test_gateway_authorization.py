import io
import unittest

from services.gateway import Handler


class AuthorizationRequest:
    def __init__(self, user):
        self.user = user
        self.response = None

    def load_session_user(self):
        return self.user

    def send_json(self, status, payload):
        self.response = (status, payload)


class GatewayAuthorizationTests(unittest.TestCase):
    def authorize(self, user, path, method="GET"):
        request = AuthorizationRequest(user)
        result = Handler.authorize(request, path, method)
        return result, request.response

    def test_public_catalog_read_does_not_require_session(self):
        result, response = self.authorize(None, "/api/catalogo/products")
        self.assertIsNone(result)
        self.assertIsNone(response)

    def test_customer_can_access_customer_domain_but_not_admin_views(self):
        customer = {"id": "customer-id", "role": "customer"}
        result, response = self.authorize(customer, "/api/pedidos/orders")
        self.assertEqual(result, customer)
        self.assertIsNone(response)
        result, response = self.authorize(customer, "/api/views/overview")
        self.assertFalse(result)
        self.assertEqual(response[0], 403)

    def test_unauthenticated_domain_access_is_rejected(self):
        result, response = self.authorize(None, "/api/pagos/payments")
        self.assertFalse(result)
        self.assertEqual(response[0], 401)

    def test_customer_cannot_write_catalog(self):
        result, response = self.authorize({"id": "customer-id", "role": "customer"},
                                          "/api/catalogo/products", "POST")
        self.assertFalse(result)
        self.assertEqual(response[0], 403)

    def test_customer_cannot_execute_admin_remediation(self):
        result, response = self.authorize({"id": "customer-id", "role": "customer"},
                                          "/api/incidents/actions/analyze-orders", "POST")
        self.assertFalse(result)
        self.assertEqual(response[0], 403)

    def test_customer_can_submit_reports_but_cannot_manage_them(self):
        customer = {"id": "customer-id", "role": "customer"}
        result, response = self.authorize(customer, "/api/reports", "POST")
        self.assertEqual(result, customer)
        self.assertIsNone(response)
        result, response = self.authorize(customer, "/api/reports/report-id", "PATCH")
        self.assertFalse(result)
        self.assertEqual(response[0], 403)

    def test_admin_can_manage_reports(self):
        admin = {"id": "admin-id", "role": "admin"}
        result, response = self.authorize(admin, "/api/reports/report-id", "PATCH")
        self.assertEqual(result, admin)
        self.assertIsNone(response)

    def test_auth_responses_record_the_real_status_for_structured_logs(self):
        class AuthResponse:
            def __init__(self):
                self.status = None
                self.headers = []
                self.wfile = io.BytesIO()

            def send_response(self, status):
                self.status = status

            def send_header(self, name, value):
                self.headers.append((name, value))

            def end_headers(self):
                pass

        response = AuthResponse()
        Handler.send_auth_json(response, 201, {"ok": True})
        self.assertEqual(response.status, 201)
        self.assertEqual(response._response_status, 201)
        self.assertIn(b'"ok": true', response.wfile.getvalue())


if __name__ == "__main__":
    unittest.main()
