"""Read-only API views composed from telemetry and incident services."""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from services.common import JsonHandler

TELEMETRY_URL = os.environ.get("TELEMETRY_URL", "http://127.0.0.1:8001")
INCIDENTS_URL = os.environ.get("INCIDENTS_URL", "http://127.0.0.1:8002")
TIMEOUT_SECONDS = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "4"))
SERVICES = ("pedidos", "pagos", "calificaciones", "catalogo")

def get_json(url):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return exc.code, {"error": "error de servicio interno"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ConnectionError(str(exc)) from exc

def load_read_model():
    code, summary = get_json(TELEMETRY_URL + "/telemetry/summary")
    if code != 200:
        raise ConnectionError("Telemetry Service no disponible")
    code, incidents_data = get_json(INCIDENTS_URL + "/incidents?status=open")
    if code != 200:
        raise ConnectionError("Incident Service no disponible")
    latest = {row["service"]: row for row in summary.get("latest_by_service", [])}
    active = {row["service"]: row for row in incidents_data.get("incidents", [])}
    service_views = []
    for service in SERVICES:
        metric = latest.get(service)
        incident = active.get(service)
        request_count = metric.get("request_count", 0) if metric else 0
        error_count = metric.get("error_count", 0) if metric else 0
        pool_active = metric.get("db_pool_active") if metric else None
        pool_max = metric.get("db_pool_max") if metric else None
        service_views.append({
            "service": service,
            "status": incident["severity"] if incident else ("ok" if metric else "no_data"),
            "latest_measurement": metric,
            "error_rate": round(error_count / request_count, 4) if request_count else 0.0,
            "db_pool_ratio": round(pool_active / pool_max, 4) if pool_active is not None and pool_max else None,
            "open_incident": incident,
        })
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "measurement_count": summary.get("measurement_count", 0),
        "open_incident_count": len(incidents_data.get("incidents", [])),
        "services": service_views,
    }

class Handler(JsonHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health/detailed":
            dependencies = {}
            for name, base in (("telemetry", TELEMETRY_URL), ("incidents", INCIDENTS_URL)):
                try:
                    code, body = get_json(base + "/health/detailed")
                    dependencies[name] = body
                    if code != 200: dependencies[name]["status"] = "unavailable"
                except ConnectionError as exc:
                    dependencies[name] = {"status": "unavailable", "error": str(exc)}
            healthy = all(item.get("status") == "ok" for item in dependencies.values())
            self.send_json(200 if healthy else 503, {"service": "views", "status": "ok" if healthy else "degraded",
                                                     "dependencies": dependencies, "database": "not used by views service"})
            return
        if parsed.path == "/health":
            states = {}
            for name, base in (("telemetry", TELEMETRY_URL), ("incidents", INCIDENTS_URL)):
                try:
                    code, body = get_json(base + "/health")
                    states[name] = "ok" if code == 200 else "unavailable"
                except ConnectionError:
                    states[name] = "unavailable"
            healthy = all(state == "ok" for state in states.values())
            self.send_json(200 if healthy else 503, {"service": "views", "status": "ok" if healthy else "degraded", "dependencies": states})
            return
        try:
            read_model = load_read_model()
        except ConnectionError as exc:
            self.send_json(503, {"error": "vista temporalmente no disponible", "details": str(exc)})
            return
        if parsed.path == "/views/overview":
            self.send_json(200, read_model)
            return
        if parsed.path == "/views/services":
            self.send_json(200, {"generated_at": read_model["generated_at"], "services": read_model["services"]})
            return
        if parsed.path == "/views/incidents":
            status_filter = parse_qs(parsed.query).get("status", ["open"])[0]
            if status_filter not in {"open", "resolved", "all"}:
                self.send_json(400, {"error": "status debe ser open, resolved o all"})
                return
            code, data = get_json(INCIDENTS_URL + "/incidents?" + urlencode({"status": status_filter}))
            self.send_json(code, {"generated_at": read_model["generated_at"], "incidents": data.get("incidents", [])})
            return
        self.send_json(404, {"error": "vista no encontrada", "views": ["/views/overview", "/views/services", "/views/incidents"]})

def main():
    port = int(os.environ.get("PORT", "8003"))
    print(f"views service listening on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

if __name__ == "__main__":
    main()
