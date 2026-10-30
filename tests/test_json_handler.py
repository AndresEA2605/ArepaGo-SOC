import io
import unittest
from email.message import Message

from services.common import JsonHandler


class JsonHandlerTests(unittest.TestCase):
    def request(self, body):
        handler = JsonHandler.__new__(JsonHandler)
        handler.headers = Message()
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        return handler

    def test_reads_json_object(self):
        self.assertEqual(self.request(b'{"ok":true}').read_json(), {"ok": True})

    def test_rejects_arrays_and_empty_body(self):
        with self.assertRaises(ValueError):
            self.request(b'[1,2]').read_json()
        with self.assertRaises(ValueError):
            self.request(b'').read_json()

    def test_http_request_logging_is_structured(self):
        # Regression: log_message used to be nested inside log_event, so
        # BaseHTTPRequestHandler fell back to its unstructured default.
        self.assertIn("log_message", JsonHandler.__dict__)


if __name__ == "__main__":
    unittest.main()
