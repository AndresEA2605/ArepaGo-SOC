"""Small shared helpers for the dependency-free HTTP services."""
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import uuid
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

_LOG_QUEUE = queue.Queue(maxsize=1000)
_LOG_WORKER_LOCK = threading.Lock()
_LOG_WORKER_STARTED = False


def _safe_log_route(value):
    route = urlparse(str(value or "/")).path
    route = re.sub(r"/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}(?=/|$)", "/{id}", route)
    route = re.sub(r"/\d+(?=/|$)", "/{id}", route)
    return route[:160]


def _persist_log(record):
    global _LOG_WORKER_STARTED
    if os.environ.get("SERVICE_NAME") == "telemetry" or not os.environ.get("TELEMETRY_URL"):
        return
    clean = dict(record)
    clean["service"] = str(clean.get("service") or os.environ.get("SERVICE_NAME") or "unknown")[:40]
    clean["route"] = _safe_log_route(clean.get("route"))
    clean["message"] = str(clean.get("message") or clean.get("error") or "")[:4000]
    clean["stack_trace"] = str(clean.get("stack_trace") or "")[-16000:]
    try:
        _LOG_QUEUE.put_nowait(clean)
    except queue.Full:
        return
    with _LOG_WORKER_LOCK:
        if not _LOG_WORKER_STARTED:
            threading.Thread(target=_log_worker, name="structured-log-forwarder", daemon=True).start()
            _LOG_WORKER_STARTED = True


def _log_worker():
    while True:
        record = _LOG_QUEUE.get()
        try:
            body = json.dumps(record, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(os.environ["TELEMETRY_URL"].rstrip("/") + "/logs",
                data=body, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=0.5) as response:
                response.read(64)
        except Exception:
            pass
        finally:
            _LOG_QUEUE.task_done()

class JsonHandler(BaseHTTPRequestHandler):
    server_version = "ArepaGoSOC/0.1"

    def parse_request(self):
        self._request_started = time.perf_counter()
        parsed = super().parse_request()
        candidate = self.headers.get("X-Request-Id") if self.headers else None
        try:
            self._request_id = str(uuid.UUID(candidate)) if candidate else str(uuid.uuid4())
        except ValueError:
            self._request_id = str(uuid.uuid4())
        return parsed

    def send_json(self, status, payload):
        self._response_status = status
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        elapsed_ms = round((time.perf_counter() - getattr(self, "_request_started", time.perf_counter())) * 1000, 3)
        route = re.sub(r"/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", "/{id}", urlparse(getattr(self, "path", "/")).path)
        service = os.environ.get("SERVICE_NAME") or os.environ.get("APPLICATION_NAME") or "arepago-service"
        status = getattr(self, "_response_status", 0)
        record = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "level": "error" if status >= 500 else "info",
                  "event": "http_request", "service": service, "request_id": getattr(self, "_request_id", None),
                  "method": getattr(self, "command", None), "route": route, "status": status,
                  "duration_ms": elapsed_ms, "message": fmt % args}
        sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        if status >= 500:
            _persist_log(record)

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

    def log_exception(self, exc, event="request_failed"):
        record = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": "error",
                  "event": event, "service": os.environ.get("SERVICE_NAME") or "arepago-service",
                  "request_id": getattr(self, "_request_id", None), "method": getattr(self, "command", None),
                  "route": urlparse(getattr(self, "path", "/")).path,
                  "error_type": type(exc).__name__, "error": str(exc), "stack_trace": traceback.format_exc()}
        sys.stderr.write(json.dumps(record, ensure_ascii=False) + "\n")
        sys.stderr.flush()
        _persist_log(record)


def log_event(event, level="info", **fields):
    record = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "level": level, "event": event, **fields}
    stream = sys.stderr if level in {"error", "critical"} else sys.stdout
    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    stream.flush()
    if level in {"warning", "error", "critical"}:
        _persist_log(record)
