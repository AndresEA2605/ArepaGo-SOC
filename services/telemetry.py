"""PostgreSQL-backed telemetry ingestion and query service."""
import os
import json
import uuid
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from psycopg.types.json import Jsonb
from services.common import JsonHandler
from services.db import connection, init_pool, pool_stats

SERVICES = {"pedidos", "pagos", "calificaciones", "catalogo"}
SCHEMA = "soc_telemetry"
LOG_SERVICES = SERVICES | {"transaction-router", "views", "incidents", "auth", "reports"}
LOG_RETENTION_DAYS = max(1, min(365, int(os.environ.get("LOG_RETENTION_DAYS", "30"))))
_last_log_prune = 0.0
_log_prune_lock = threading.Lock()

def initialize():
    init_pool("arepago-telemetry")
    with connection() as db:
        db.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        db.execute(f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.measurements (
            id UUID PRIMARY KEY,
            timestamp TIMESTAMPTZ NOT NULL,
            service TEXT NOT NULL CHECK (service IN ('pedidos','pagos','calificaciones','catalogo')),
            environment TEXT NOT NULL,
            latency_p95_ms DOUBLE PRECISION NOT NULL CHECK (latency_p95_ms >= 0),
            request_count BIGINT NOT NULL CHECK (request_count >= 0),
            error_count BIGINT NOT NULL CHECK (error_count >= 0 AND error_count <= request_count),
            db_pool_active INTEGER,
            db_pool_max INTEGER,
            source TEXT NOT NULL,
            diagnostics JSONB NOT NULL DEFAULT '[]'::jsonb,
            CHECK ((db_pool_active IS NULL AND db_pool_max IS NULL) OR
                   (db_pool_active >= 0 AND db_pool_max >= db_pool_active AND db_pool_max > 0))
        )""")
        db.execute(f"ALTER TABLE {SCHEMA}.measurements ADD COLUMN IF NOT EXISTS diagnostics JSONB NOT NULL DEFAULT '[]'::jsonb")
        db.execute(f"CREATE INDEX IF NOT EXISTS measurements_service_time ON {SCHEMA}.measurements(service, timestamp DESC)")
        db.execute(f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.logs (
            id UUID PRIMARY KEY,
            timestamp TIMESTAMPTZ NOT NULL,
            level TEXT NOT NULL CHECK (level IN ('warning','error','critical')),
            service TEXT NOT NULL,
            event TEXT NOT NULL,
            request_id TEXT,
            method TEXT,
            route TEXT,
            status INTEGER,
            duration_ms DOUBLE PRECISION,
            error_type TEXT,
            message TEXT NOT NULL DEFAULT '',
            stack_trace TEXT NOT NULL DEFAULT '',
            details JSONB NOT NULL DEFAULT '{{}}'::jsonb
        )""")
        db.execute(f"CREATE INDEX IF NOT EXISTS logs_service_time ON {SCHEMA}.logs(service, timestamp DESC)")

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def validate(data):
    service = str(data.get("service", "")).strip().lower()
    if service not in SERVICES:
        raise ValueError("service debe ser pedidos, pagos, calificaciones o catalogo")
    timestamp = data.get("timestamp") or now()
    if not isinstance(timestamp, str) or len(timestamp) > 64:
        raise ValueError("timestamp debe ser una fecha en formato ISO 8601")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed_timestamp.tzinfo is None:
            parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)
        timestamp = parsed_timestamp.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError as exc:
        raise ValueError("timestamp debe ser una fecha en formato ISO 8601") from exc
    try:
        measurement_id = str(uuid.UUID(str(data.get("id") or uuid.uuid4())))
    except ValueError as exc:
        raise ValueError("id debe ser un UUID") from exc
    try:
        latency = float(data["latency_p95_ms"])
        requests, errors = int(data["request_count"]), int(data["error_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("latency_p95_ms, request_count y error_count son obligatorios y numéricos") from exc
    if latency < 0 or requests < 0 or errors < 0 or errors > requests:
        raise ValueError("latencia y contadores deben ser positivos; error_count no puede superar request_count")
    active, maximum = data.get("db_pool_active"), data.get("db_pool_max")
    if (active is None) != (maximum is None):
        raise ValueError("db_pool_active y db_pool_max deben enviarse juntos")
    if active is not None:
        try:
            active, maximum = int(active), int(maximum)
        except (TypeError, ValueError) as exc:
            raise ValueError("Los valores del pool deben ser enteros") from exc
        if active < 0 or maximum < 1 or active > maximum:
            raise ValueError("El pool debe cumplir 0 <= db_pool_active <= db_pool_max")
    diagnostics = data.get("diagnostics", [])
    if not isinstance(diagnostics, list) or len(diagnostics) > 20 or any(not isinstance(item, dict) for item in diagnostics):
        raise ValueError("diagnostics debe ser una lista de hasta 20 rutas")
    if len(json.dumps(diagnostics, ensure_ascii=False)) > 16_384:
        raise ValueError("diagnostics excede el tamaño permitido")
    return {"id": measurement_id, "timestamp": timestamp,
        "service": service, "environment": str(data.get("environment") or "production")[:64],
        "latency_p95_ms": latency, "request_count": requests, "error_count": errors,
        "db_pool_active": active, "db_pool_max": maximum, "source": str(data.get("source") or "api")[:128],
        "diagnostics": diagnostics}

def as_dict(row):
    result = dict(row)
    result["id"] = str(result["id"])
    result["timestamp"] = result["timestamp"].isoformat()
    return result


def validate_log(data):
    service = str(data.get("service", "")).strip().lower()
    level = str(data.get("level", "")).strip().lower()
    event = str(data.get("event", "")).strip()[:100]
    if service not in LOG_SERVICES or level not in {"warning", "error", "critical"} or not event:
        raise ValueError("log debe incluir service, level de advertencia/error y event válidos")
    timestamp = data.get("timestamp") or now()
    if not isinstance(timestamp, str) or len(timestamp) > 64:
        raise ValueError("timestamp de log inválido")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp de log inválido") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    try:
        log_id = str(uuid.UUID(str(data.get("id") or uuid.uuid4())))
    except ValueError as exc:
        raise ValueError("id de log inválido") from exc
    status = data.get("status")
    if status is not None:
        status = int(status)
        if not 100 <= status <= 599:
            raise ValueError("status HTTP de log inválido")
    duration = data.get("duration_ms")
    if duration is not None:
        duration = float(duration)
        if duration < 0:
            raise ValueError("duration_ms de log inválido")
    core = {"timestamp", "level", "service", "event", "request_id", "method", "route", "status", "duration_ms", "error_type", "error", "message", "stack_trace", "id"}
    details = {key: value for key, value in data.items() if key not in core}
    if len(json.dumps(details, ensure_ascii=False, default=str)) > 8000:
        raise ValueError("details de log excede el tamaño permitido")
    route = str(data.get("route") or "")[:160].split("?", 1)[0]
    return {"id": log_id, "timestamp": parsed, "level": level, "service": service, "event": event,
        "request_id": str(data.get("request_id") or "")[:80] or None,
        "method": str(data.get("method") or "")[:8] or None, "route": route or None, "status": status,
        "duration_ms": duration, "error_type": str(data.get("error_type") or "")[:120] or None,
        "message": str(data.get("message") or data.get("error") or "")[:4000],
        "stack_trace": str(data.get("stack_trace") or "")[-16000:], "details": details}


def public_log(row):
    item = dict(row)
    item["id"] = str(item["id"])
    item["timestamp"] = item["timestamp"].isoformat()
    return item


def parse_log_time(value, fallback):
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError("fecha de filtro inválida; usa ISO 8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def prune_logs_if_due(db):
    global _last_log_prune
    now_mono = time.monotonic()
    if now_mono - _last_log_prune < 3600:
        return
    with _log_prune_lock:
        now_mono = time.monotonic()
        if now_mono - _last_log_prune >= 3600:
            db.execute(f"DELETE FROM {SCHEMA}.logs WHERE timestamp < now() - (%s * INTERVAL '1 day')",
                       (LOG_RETENTION_DAYS,))
            _last_log_prune = now_mono

class Handler(JsonHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/health", "/health/detailed"}:
            try:
                with connection() as db:
                    db.execute("SELECT 1")
                details = {"service": "telemetry", "status": "ok", "database": "connected"}
                if parsed.path == "/health/detailed": details["database_pool"] = pool_stats()
                self.send_json(200, details)
            except Exception as exc:
                self.log_exception(exc, "health_check_failed")
                self.send_json(503, {"service": "telemetry", "status": "degraded", "database": "unavailable",
                                     "diagnostic": type(exc).__name__, "database_pool": pool_stats()})
            return
        if parsed.path == "/telemetry/summary":
            with connection() as db:
                rows = db.execute(f"""SELECT DISTINCT ON (service) * FROM {SCHEMA}.measurements
                    ORDER BY service, timestamp DESC""").fetchall()
                total = db.execute(f"SELECT COUNT(*) AS total FROM {SCHEMA}.measurements").fetchone()["total"]
            self.send_json(200, {"measurement_count": total, "latest_by_service": [as_dict(row) for row in rows]})
            return
        # Public gateway URLs retain the /telemetry prefix when proxied, while
        # internal log forwarders post directly to /logs. Accept both paths.
        if parsed.path in {"/logs", "/telemetry/logs"}:
            query = parse_qs(parsed.query)
            service = query.get("service", [None])[0]
            if service not in LOG_SERVICES:
                self.send_json(400, {"error": "service de logs no válido"})
                return
            now_utc = datetime.now(timezone.utc)
            try:
                start = parse_log_time(query.get("from", [None])[0], now_utc - timedelta(hours=1))
                end = parse_log_time(query.get("to", [None])[0], now_utc)
                limit = max(1, min(200, int(query.get("limit", ["100"])[0])))
                if start > end or end - start > timedelta(days=31):
                    raise ValueError("el intervalo de logs debe ser válido y no superar 31 días")
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            with connection() as db:
                rows = db.execute(f"""SELECT * FROM {SCHEMA}.logs
                    WHERE service=%s AND timestamp >= %s AND timestamp <= %s
                    ORDER BY timestamp DESC LIMIT %s""", (service, start, end, limit)).fetchall()
            self.send_json(200, {"service": service, "from": start.isoformat(), "to": end.isoformat(),
                                 "logs": [public_log(row) for row in rows]})
            return
        if parsed.path == "/telemetry":
            query = parse_qs(parsed.query)
            service, limit_text = query.get("service", [None])[0], query.get("limit", ["100"])[0]
            try:
                limit = max(1, min(500, int(limit_text)))
                start = parse_log_time(query.get("from", [None])[0], None)
                end = parse_log_time(query.get("to", [None])[0], None)
            except ValueError:
                self.send_json(400, {"error": "limit o intervalo temporal inválido"})
                return
            if service and service not in SERVICES:
                self.send_json(400, {"error": "service desconocido"})
                return
            if start and end and (start > end or end - start > timedelta(days=366)):
                self.send_json(400, {"error": "el intervalo de mediciones debe ser válido y no superar un año"})
                return
            where, params = [], []
            if service:
                where.append("service=%s"); params.append(service)
            if start:
                where.append("timestamp >= %s"); params.append(start)
            if end:
                where.append("timestamp <= %s"); params.append(end)
            where_sql = " WHERE " + " AND ".join(where) if where else ""
            params.append(limit)
            with connection() as db:
                rows = db.execute(f"SELECT * FROM {SCHEMA}.measurements{where_sql} ORDER BY timestamp DESC LIMIT %s", tuple(params)).fetchall()
            self.send_json(200, {"measurements": [as_dict(row) for row in rows]})
            return
        self.send_json(404, {"error": "ruta no encontrada"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path in {"/logs", "/telemetry/logs"}:
            try:
                item = validate_log(self.read_json())
            except (ValueError, TypeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            with connection() as db:
                db.execute(f"""INSERT INTO {SCHEMA}.logs
                    (id,timestamp,level,service,event,request_id,method,route,status,duration_ms,error_type,message,stack_trace,details)
                    VALUES (%(id)s,%(timestamp)s,%(level)s,%(service)s,%(event)s,%(request_id)s,%(method)s,%(route)s,
                    %(status)s,%(duration_ms)s,%(error_type)s,%(message)s,%(stack_trace)s,%(details)s)""",
                    {**item, "details": Jsonb(item["details"])})
                prune_logs_if_due(db)
            self.send_json(201, {"id": item["id"], "stored": True})
            return
        if path != "/telemetry":
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        try:
            metric = validate(self.read_json())
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        with connection() as db:
            db.execute(f"""INSERT INTO {SCHEMA}.measurements
                (id,timestamp,service,environment,latency_p95_ms,request_count,error_count,db_pool_active,db_pool_max,source,diagnostics)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (
                metric["id"], datetime.fromisoformat(metric["timestamp"]), metric["service"], metric["environment"], metric["latency_p95_ms"],
                metric["request_count"], metric["error_count"], metric["db_pool_active"], metric["db_pool_max"], metric["source"],
                Jsonb(metric["diagnostics"])))
        self.send_json(201, metric)

def main():
    initialize()
    port = int(os.environ.get("PORT", "8001"))
    print(f"telemetry service listening on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

if __name__ == "__main__":
    main()
