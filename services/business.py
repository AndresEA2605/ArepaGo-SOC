"""Scaffold for an ArepaGo domain microservice behind its own load balancer."""
import os
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse
from services.common import JsonHandler

SERVICE_NAME = os.environ.get("SERVICE_NAME", "servicio")
PORT = int(os.environ.get("PORT", "8000"))

class Handler(JsonHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self.send_json(200, {"service": SERVICE_NAME, "status": "ok"})
        else:
            self.send_json(501, {"error": "domain_service_not_implemented", "service": SERVICE_NAME,
                "message": "El balanceador y el enrutamiento están configurados; la lógica de negocio de este servicio se implementará en la siguiente fase."})

    def do_POST(self):
        self.send_json(501, {"error": "domain_service_not_implemented", "service": SERVICE_NAME,
            "message": "La lógica de negocio de este servicio se implementará en la siguiente fase."})

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

def main():
    print(f"{SERVICE_NAME} service listening on :{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
