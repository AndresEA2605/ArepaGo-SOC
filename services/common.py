"""Small shared helpers for the dependency-free HTTP services."""
import json
from http.server import BaseHTTPRequestHandler

class JsonHandler(BaseHTTPRequestHandler):
    server_version = "ArepaGoSOC/0.1"

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Content-Length inválido")
        if length <= 0 or length > 1_000_000:
            raise ValueError("El cuerpo debe tener entre 1 byte y 1 MB")
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("JSON inválido") from exc
        if not isinstance(value, dict):
            raise ValueError("El cuerpo debe ser un objeto JSON")
        return value

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))
