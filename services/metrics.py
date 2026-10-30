"""Process-local HTTP measurements collected from real gateway requests."""
import threading
import time
from collections import defaultdict, deque
import re
from urllib.parse import urlparse


class RequestMetrics:
    def __init__(self, window_seconds=15):
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._requests = defaultdict(int)
        self._errors = defaultdict(int)
        self._latencies = defaultdict(deque)
        self._routes = defaultdict(lambda: defaultdict(lambda: {"requests": 0, "errors": 0, "statuses": defaultdict(int)}))
        self._last_snapshot = time.time()

    @staticmethod
    def _safe_route(path):
        route = urlparse(path or "/").path
        route = re.sub(r"/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}(?=/|$)", "/{id}", route)
        route = re.sub(r"/\d+(?=/|$)", "/{id}", route)
        return route[:160] or "/"

    def observe(self, service, duration_ms, status, method="GET", path="/"):
        if service not in {"pedidos", "pagos", "calificaciones", "catalogo"}:
            return
        now = time.monotonic()
        with self._lock:
            self._requests[service] += 1
            self._errors[service] += int(status >= 500)
            route_name = self._safe_route(path)
            route_key = (str(method)[:8], route_name)
            if route_key not in self._routes[service] and len(self._routes[service]) >= 200:
                route_key = (str(method)[:8], "/{other}")
            route = self._routes[service][route_key]
            route["requests"] += 1
            route["errors"] += int(status >= 500)
            route["statuses"][str(int(status))] += 1
            values = self._latencies[service]
            values.append((now, duration_ms))
            cutoff = now - self.window_seconds
            while values and values[0][0] < cutoff:
                values.popleft()

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            result = {}
            for service in ("pedidos", "pagos", "calificaciones", "catalogo"):
                observations = sorted(value for _, value in self._latencies[service])
                p95 = observations[max(0, int(len(observations) * .95 + .999) - 1)] if observations else 0.0
                result[service] = {"request_count": self._requests.pop(service, 0),
                    "error_count": self._errors.pop(service, 0), "latency_p95_ms": round(p95, 3),
                    "observation_count": len(observations),
                    "route_diagnostics": [{"method": method, "route": route, **values,
                        "statuses": dict(values["statuses"])}
                        for (method, route), values in sorted(self._routes.pop(service, {}).items(),
                            key=lambda item: (item[1]["errors"], item[1]["requests"]), reverse=True)[:20]]}
                self._latencies[service].clear()
            interval = max(0.0, now - self._last_snapshot)
            self._last_snapshot = now
        return {"interval_seconds": round(interval, 3), "services": result}

    def current(self):
        with self._lock:
            return [{"service": service, "requests_since_sample": self._requests.get(service, 0),
                    "errors_since_sample": self._errors.get(service, 0),
                    "latency_observations": len(self._latencies[service])}
                    for service in ("pedidos", "pagos", "calificaciones", "catalogo")]


request_metrics = RequestMetrics()
