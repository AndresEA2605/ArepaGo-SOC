"""Transaction router: routes telemetry and ArepaGo domain traffic."""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse
from services.common import JsonHandler, log_event
from services.internal_auth import IDENTITY_HEADER, load_secret, sign_identity
from services.metrics import request_metrics
from services.rate_limit import resolve_client_ip

TELEMETRY_URL = os.environ.get("TELEMETRY_URL", "http://127.0.0.1:8001")
INCIDENTS_URL = os.environ.get("INCIDENTS_URL", "http://127.0.0.1:8002")
VIEWS_URL = os.environ.get("VIEWS_URL", "http://127.0.0.1:8003")
AUTH_URL = os.environ.get("AUTH_URL", "http://127.0.0.1:8004")
REPORTS_URL = os.environ.get("REPORTS_URL", "http://127.0.0.1:8005")
DOMAIN_ROUTES = {
    "/api/pedidos": os.environ.get("PEDIDOS_ALB_URL", "http://127.0.0.1:8101"),
    "/api/pagos": os.environ.get("PAGOS_ALB_URL", "http://127.0.0.1:8102"),
    "/api/calificaciones": os.environ.get("CALIFICACIONES_ALB_URL", "http://127.0.0.1:8103"),
    "/api/catalogo": os.environ.get("CATALOGO_ALB_URL", "http://127.0.0.1:8104"),
}
TIMEOUT_SECONDS = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "4"))
PRODUCTION_ENVIRONMENTS = {"production", "prod"}


def origin_is_allowed(origin, host, scheme, has_cookie):
    """Allow same-origin browser requests and cookie-less API clients.

    A cross-site form or fetch always carries ``Origin`` in current browsers.
    API clients that use ``Authorization: Bearer`` do not carry cookies, so
    CSRF does not apply and an absent ``Origin`` is safe to accept.
    """
    if not origin:
        return not has_cookie
    parsed_origin = urlparse(origin)
    return (parsed_origin.scheme.lower() == (scheme or "").lower()
            and parsed_origin.netloc.lower() == (host or "").lower())


def metrics_payload():
    return {"source": "transaction-router process memory", "services": request_metrics.current()}


def validate_runtime_config():
    """Fail fast when a production deployment is missing mandatory safeguards."""
    environment = os.environ.get("APP_ENV", "local").strip().lower()
    if environment in PRODUCTION_ENVIRONMENTS:
        secure_cookie = os.environ.get("AUTH_COOKIE_SECURE", "false").strip().lower()
        if secure_cookie not in {"1", "true", "yes"}:
            raise RuntimeError("AUTH_COOKIE_SECURE debe estar activo en producción")
        if not (os.environ.get("INTERNAL_AUTH_SECRET_FILE") or os.environ.get("INTERNAL_AUTH_SECRET")):
            raise RuntimeError("La identidad interna exige INTERNAL_AUTH_SECRET_FILE en producción")
    if TIMEOUT_SECONDS <= 0 or TIMEOUT_SECONDS > 60:
        raise RuntimeError("UPSTREAM_TIMEOUT_SECONDS debe estar entre 0 y 60")


def request_json(url, method="GET", payload=None, headers=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
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
        started = time.perf_counter()
        service = next((name.removeprefix("/api/") for name in DOMAIN_ROUTES
                        if self.path == name or self.path.startswith(name + "/")), None)
        self._response_status = 0
        try:
            self._route_request(method)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            status = getattr(self, "_response_status", 500) or 500
            request_metrics.observe(service, duration_ms, status,
                                    getattr(self, "command", "GET"), getattr(self, "path", "/"))
            if service and status >= 500:
                log_event("upstream_request_failed", level="error", service=service,
                          request_id=getattr(self, "_request_id", None),
                          method=getattr(self, "command", "GET"), route=urlparse(self.path).path,
                          status=status, duration_ms=duration_ms,
                          message="El gateway recibió una respuesta HTTP 5xx del servicio upstream.")

    def _route_request(self, method):
        parsed = urlparse(self.path)
        path, query = parsed.path, parsed.query
        suffix = ("?" + query) if query else ""
        if path == "/health":
            self.health()
            return
        if path == "/health/detailed":
            user = self.authorize("/api/views/overview", "GET")
            if user is False:
                return
            self.health_detailed()
            return
        if path == "/metrics":
            user = self.authorize("/api/views/overview", "GET")
            if user is False:
                return
            self.send_json(200, metrics_payload())
            return
        if path == "/":
            self.send_json(200, {"name": "ArepaGo SOC transaction router", "routes": ["/api/views/overview", "/api/views/services", "/api/views/incidents", "/api/telemetry", "/api/incidents", "/api/reports", *DOMAIN_ROUTES.keys()]})
            return
        if path in {"/api/auth/register", "/api/auth/login", "/api/auth/logout", "/api/auth/me"}:
            self.auth_route(path, method)
            return
        if "/internal/" in path or path.endswith("/internal"):
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        if path == "/api/telemetry" and method == "POST":
            if self.authorize(path, method) is False:
                return
            if not self.same_origin():
                self.send_json(403, {"error": "origen no permitido"})
                return
            self.ingest_telemetry()
            return
        if path.startswith("/api/views"):
            target = VIEWS_URL + path[len("/api"):]
        elif path.startswith("/api/telemetry"):
            target = TELEMETRY_URL + path[len("/api"):]
        elif path.startswith("/api/incidents"):
            target = INCIDENTS_URL + path[len("/api"):]
        elif path.startswith("/api/reports"):
            target = REPORTS_URL + path[len("/api"):]
        else:
            target = None
            for prefix, base in DOMAIN_ROUTES.items():
                if path == prefix or path.startswith(prefix + "/"):
                    target = base + path[len(prefix):]
                    break
            if target is None:
                self.send_json(404, {"error": "ruta no encontrada"})
                return
        user = self.authorize(path, method)
        if user is False:
            return
        if method not in {"GET", "HEAD", "OPTIONS"} and not self.same_origin():
            self.send_json(403, {"error": "origen no permitido"})
            return
        self.proxy(target + suffix, method, user)

    def same_origin(self):
        scheme = self.headers.get("X-Forwarded-Proto", "http").split(",", 1)[0].strip().lower()
        return origin_is_allowed(self.headers.get("Origin"), self.headers.get("Host"),
                                 scheme, bool(self.session_token()))

    def session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get("ag_session")
        return morsel.value if morsel else ""

    def client_ip(self):
        peer = self.client_address[0] if getattr(self, "client_address", None) else None
        return resolve_client_ip(self.headers.get("X-Real-IP"),
                                 self.headers.get("X-Forwarded-For"), peer)

    def auth_call(self, path, method="GET", payload=None, token=None):
        headers = {"X-Forwarded-For": self.client_ip()}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return request_json(AUTH_URL + path, method, payload, headers)

    def auth_route(self, path, method):
        if path == "/api/auth/register" and method == "POST":
            if not self.same_origin():
                self.send_json(403, {"error": "origen no permitido"})
                return
            try:
                payload = self.read_json()
                status, result = self.auth_call("/register", "POST", payload)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            except ConnectionError:
                self.send_json(503, {"error": "servicio de autenticación no disponible"})
                return
            self.send_auth_json(status, result)
            return
        if path == "/api/auth/login" and method == "POST":
            if not self.same_origin():
                self.send_json(403, {"error": "origen no permitido"})
                return
            try:
                payload = self.read_json()
                status, result = self.auth_call("/login", "POST", payload)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            except ConnectionError:
                self.send_json(503, {"error": "servicio de autenticación no disponible"})
                return
            token = result.pop("session_token", None)
            if status == 200 and token:
                secure = "; Secure" if os.environ.get("AUTH_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"} else ""
                max_age = int(os.environ.get("SESSION_HOURS", "12")) * 3600
                self.send_auth_json(status, result, f"ag_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}")
            else:
                self.send_auth_json(status, result)
            return
        if path == "/api/auth/me" and method == "GET":
            user = self.load_session_user()
            if user is None:
                self.send_auth_json(401, {"error": "autenticación requerida"})
            elif user is False:
                self.send_auth_json(503, {"error": "servicio de autenticación no disponible"})
            else:
                self.send_auth_json(200, {"user": user})
            return
        if path == "/api/auth/logout" and method == "POST":
            if not self.same_origin():
                self.send_json(403, {"error": "origen no permitido"})
                return
            token = self.session_token()
            try:
                if token:
                    self.auth_call("/logout", "POST", {}, token)
            except ConnectionError:
                self.send_auth_json(503, {"error": "sesión local cerrada; no se pudo confirmar la revocación en el servidor"},
                                    "ag_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
                return
            self.send_auth_json(200, {"ok": True}, "ag_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
            return
        self.send_json(405, {"error": "método no permitido"})

    def send_auth_json(self, status, payload, cookie=None):
        self._response_status = status
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def load_session_user(self):
        token = self.session_token()
        if not token:
            return None
        try:
            status, result = self.auth_call("/session", token=token)
        except ConnectionError:
            return False
        if status >= 500:
            return False
        return result.get("user") if status == 200 else None

    def authorize(self, path, method):
        if path == "/api/reports" or path.startswith("/api/reports/"):
            required_role = "admin" if method == "PATCH" else "customer_or_admin"
        elif path == "/api/catalogo" or path.startswith("/api/catalogo/"):
            if method == "GET":
                # El catálogo es público, pero si hay una sesión válida se
                # propaga la identidad para que el admin pueda ver los
                # productos retirados y republicarlos.
                user = self.load_session_user()
                return None if user is False or user is None else user
            required_role = "admin"
        elif path.startswith(("/api/views", "/api/telemetry", "/api/incidents")):
            required_role = "admin"
        elif any(path == prefix or path.startswith(prefix + "/") for prefix in DOMAIN_ROUTES):
            required_role = "customer_or_admin"
        else:
            return None
        user = self.load_session_user()
        if user is False:
            self.send_json(503, {"error": "servicio de autenticación no disponible"})
            return False
        if user is None:
            self.send_json(401, {"error": "autenticación requerida"})
            return False
        if required_role == "admin" and user["role"] != "admin":
            self.send_json(403, {"error": "se requiere rol administrador"})
            return False
        return user

    def health(self):
        dependencies = {"telemetry": TELEMETRY_URL, "incidents": INCIDENTS_URL, "views": VIEWS_URL,
                        "auth": AUTH_URL, "reports": REPORTS_URL}
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

    def health_detailed(self):
        dependencies = {"telemetry": TELEMETRY_URL, "incidents": INCIDENTS_URL, "views": VIEWS_URL,
                        "auth": AUTH_URL, "reports": REPORTS_URL,
                        **{name.removeprefix("/api/"): base for name, base in DOMAIN_ROUTES.items()}}
        result = {"service": "transaction-router", "status": "ok", "dependencies": {},
                  "request_metrics": request_metrics.current()}
        status = 200
        for name, base in dependencies.items():
            try:
                code, details = request_json(base + "/health/detailed")
                result["dependencies"][name] = details
                if code != 200:
                    status = 503
            except ConnectionError as exc:
                result["dependencies"][name] = {"status": "unavailable", "error": str(exc)}
                status = 503
        result["status"] = "ok" if status == 200 else "degraded"
        self.send_json(status, result)

    def proxy(self, target, method, user=None):
        payload = None
        if method in {"POST", "PUT", "PATCH"}:
            try:
                payload = self.read_json()
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
        try:
            headers = {"X-Request-Id": getattr(self, "_request_id", "")}
            if user is not None:
                headers[IDENTITY_HEADER] = sign_identity(user["id"], user["role"])
            if target.startswith(DOMAIN_ROUTES["/api/pedidos"]) and urlparse(target).path == "/orders" and method == "POST":
                headers = headers or {}
                idempotency_key = self.headers.get("Idempotency-Key")
                if idempotency_key:
                    headers["Idempotency-Key"] = idempotency_key
            status, data = request_json(target, method, payload, headers)
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
    validate_runtime_config()
    load_secret()
    port = int(os.environ.get("PORT", "8080"))
    sampler = threading.Thread(target=sample_metrics_forever, name="telemetry-sampler", daemon=True)
    sampler.start()
    print(f"transaction router listening on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


def sample_metrics_forever():
    interval = max(5, int(os.environ.get("METRICS_SAMPLE_SECONDS", "15")))
    while True:
        time.sleep(interval)
        snapshot = request_metrics.snapshot()
        for service, values in snapshot["services"].items():
            pool = None
            base = DOMAIN_ROUTES[f"/api/{service}"]
            try:
                code, detail = request_json(base + "/health/detailed")
                if code == 200:
                    pool = detail.get("database_pool")
            except ConnectionError:
                pass
            metric = {"service": service, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "environment": os.environ.get("APP_ENV", "local"),
                      "latency_p95_ms": values["latency_p95_ms"], "request_count": values["request_count"],
                      "error_count": values["error_count"], "diagnostics": values["route_diagnostics"],
                      "db_pool_active": pool.get("active") if pool and pool.get("status") == "ok" else None,
                      "db_pool_max": pool.get("max") if pool and pool.get("status") == "ok" else None,
                      "source": "transaction-router real request observations; DB pool sampled from one service replica"}
            try:
                code, stored = request_json(TELEMETRY_URL + "/telemetry", "POST", metric)
                if code == 201:
                    request_json(INCIDENTS_URL + "/evaluate", "POST", stored)
            except ConnectionError as exc:
                sys.stderr.write(json.dumps({"level":"error","event":"telemetry_sample_failed","service":service,"error":str(exc)})+"\n")
                sys.stderr.flush()

if __name__ == "__main__":
    main()
