"""Transaction router: routes telemetry and ArepaGo domain traffic."""
import json
import os
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse
from services.common import JsonHandler

TELEMETRY_URL = os.environ.get("TELEMETRY_URL", "http://127.0.0.1:8001")
INCIDENTS_URL = os.environ.get("INCIDENTS_URL", "http://127.0.0.1:8002")
VIEWS_URL = os.environ.get("VIEWS_URL", "http://127.0.0.1:8003")
DOMAIN_ROUTES = {
    "/api/pedidos": os.environ.get("PEDIDOS_ALB_URL", "http://127.0.0.1:8101"),
    "/api/pagos": os.environ.get("PAGOS_ALB_URL", "http://127.0.0.1:8102"),
    "/api/calificaciones": os.environ.get("CALIFICACIONES_ALB_URL", "http://127.0.0.1:8103"),
    "/api/catalogo": os.environ.get("CATALOGO_ALB_URL", "http://127.0.0.1:8104"),
}
TIMEOUT_SECONDS = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "4"))

def request_json(url, method="GET", payload=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return exc.code, {"error": "error del servicio interno"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ConnectionError(str(exc)) from exc

class Handler(JsonHandler):
    def do_GET(self):
        self.route_request("GET")

    def do_POST(self):
        self.route_request("POST")

    def do_PUT(self):
        self.route_request("PUT")

    def do_PATCH(self):
        self.route_request("PATCH")

    def do_DELETE(self):
        self.route_request("DELETE")

    def route_request(self, method):
        parsed = urlparse(self.path)
        path, query = parsed.path, parsed.query
        suffix = ("?" + query) if query else ""
        if path == "/health":
            self.health()
            return
        if path == "/":
            self.send_json(200, {"name": "ArepaGo SOC transaction router", "routes": ["/api/views/overview", "/api/views/services", "/api/views/incidents", "/api/telemetry", "/api/incidents", *DOMAIN_ROUTES.keys()]})
            return
        if path == "/api/telemetry" and method == "POST":
            self.ingest_telemetry()
            return
        if path.startswith("/api/views"):
            target = VIEWS_URL + path[len("/api"):]
        elif path.startswith("/api/telemetry"):
            target = TELEMETRY_URL + path[len("/api"):]
        elif path.startswith("/api/incidents"):
            target = INCIDENTS_URL + path[len("/api"):]
        else:
            target = None
            for prefix, base in DOMAIN_ROUTES.items():
                if path == prefix or path.startswith(prefix + "/"):
                    target = base + path[len(prefix):]
                    break
            if target is None:
                self.send_json(404, {"error": "ruta no encontrada"})
                return
        self.proxy(target + suffix, method)

    def health(self):
        dependencies = {"telemetry": TELEMETRY_URL, "incidents": INCIDENTS_URL, "views": VIEWS_URL}
        dependencies.update({name.removeprefix("/"): base for name, base in DOMAIN_ROUTES.items()})
        result, status = {"service": "transaction-router", "status": "ok", "dependencies": {}}, 200
        for name, base in dependencies.items():
            try:
                code, payload = request_json(base + "/health")
                result["dependencies"][name] = {"status": "ok" if code == 200 else "unavailable", "details": payload}
                if code != 200:
                    status = 503
            except ConnectionError as exc:
                result["dependencies"][name] = {"status": "unavailable", "error": str(exc)}
                status = 503
        result["status"] = "ok" if status == 200 else "degraded"
        self.send_json(status, result)

    def proxy(self, target, method):
        payload = None
        if method in {"POST", "PUT", "PATCH"}:
            try:
                payload = self.read_json()
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
        try:
            status, data = request_json(target, method, payload)
            self.send_json(status, data)
        except ConnectionError as exc:
            self.send_json(503, {"error": "servicio interno no disponible", "details": str(exc)})

    def ingest_telemetry(self):
        try:
            metric = self.read_json()
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        try:
            status, stored = request_json(TELEMETRY_URL + "/telemetry", "POST", metric)
            if status >= 400:
                self.send_json(status, stored)
                return
            eval_status, evaluation = request_json(INCIDENTS_URL + "/evaluate", "POST", stored)
            if eval_status >= 400:
                self.send_json(502, {"measurement": stored, "error": "la medición se guardó, pero la evaluación falló", "incident_service": evaluation})
                return
            self.send_json(201, {"measurement": stored, "incident_evaluation": evaluation})
        except ConnectionError as exc:
            self.send_json(503, {"error": "servicio interno no disponible", "details": str(exc)})

def main():
    port = int(os.environ.get("PORT", "8080"))
    print(f"transaction router listening on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

if __name__ == "__main__":
    main()
