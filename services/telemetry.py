"""PostgreSQL-backed telemetry ingestion and query service."""
import os
import uuid
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from services.common import JsonHandler
from services.db import connection, init_pool

SERVICES = {"pedidos", "pagos", "calificaciones", "catalogo"}
SCHEMA = "soc_telemetry"

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
            CHECK ((db_pool_active IS NULL AND db_pool_max IS NULL) OR
                   (db_pool_active >= 0 AND db_pool_max >= db_pool_active AND db_pool_max > 0))
        )""")
        db.execute(f"CREATE INDEX IF NOT EXISTS measurements_service_time ON {SCHEMA}.measurements(service, timestamp DESC)")

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
    return {"id": measurement_id, "timestamp": timestamp,
        "service": service, "environment": str(data.get("environment") or "production")[:64],
        "latency_p95_ms": latency, "request_count": requests, "error_count": errors,
        "db_pool_active": active, "db_pool_max": maximum, "source": str(data.get("source") or "api")[:128]}

def as_dict(row):
    result = dict(row)
    result["id"] = str(result["id"])
    result["timestamp"] = result["timestamp"].isoformat()
    return result

class Handler(JsonHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            try:
                with connection() as db:
                    db.execute("SELECT 1")
                self.send_json(200, {"service": "telemetry", "status": "ok", "database": "connected"})
            except Exception:
                self.send_json(503, {"service": "telemetry", "status": "degraded", "database": "unavailable"})
            return
        if parsed.path == "/telemetry/summary":
            with connection() as db:
                rows = db.execute(f"""SELECT DISTINCT ON (service) * FROM {SCHEMA}.measurements
                    ORDER BY service, timestamp DESC""").fetchall()
                total = db.execute(f"SELECT COUNT(*) AS total FROM {SCHEMA}.measurements").fetchone()["total"]
            self.send_json(200, {"measurement_count": total, "latest_by_service": [as_dict(row) for row in rows]})
            return
        if parsed.path == "/telemetry":
            query = parse_qs(parsed.query)
            service, limit_text = query.get("service", [None])[0], query.get("limit", ["100"])[0]
            try:
                limit = max(1, min(500, int(limit_text)))
            except ValueError:
                self.send_json(400, {"error": "limit debe ser un entero entre 1 y 500"})
                return
            if service and service not in SERVICES:
                self.send_json(400, {"error": "service desconocido"})
                return
            with connection() as db:
                if service:
                    rows = db.execute(f"SELECT * FROM {SCHEMA}.measurements WHERE service=%s ORDER BY timestamp DESC LIMIT %s", (service, limit)).fetchall()
                else:
                    rows = db.execute(f"SELECT * FROM {SCHEMA}.measurements ORDER BY timestamp DESC LIMIT %s", (limit,)).fetchall()
            self.send_json(200, {"measurements": [as_dict(row) for row in rows]})
            return
        self.send_json(404, {"error": "ruta no encontrada"})

    def do_POST(self):
        if urlparse(self.path).path != "/telemetry":
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        try:
            metric = validate(self.read_json())
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        with connection() as db:
            db.execute(f"""INSERT INTO {SCHEMA}.measurements
                (id,timestamp,service,environment,latency_p95_ms,request_count,error_count,db_pool_active,db_pool_max,source)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (
                metric["id"], datetime.fromisoformat(metric["timestamp"]), metric["service"], metric["environment"], metric["latency_p95_ms"],
                metric["request_count"], metric["error_count"], metric["db_pool_active"], metric["db_pool_max"], metric["source"]))
        self.send_json(201, metric)

def main():
    initialize()
    port = int(os.environ.get("PORT", "8001"))
    print(f"telemetry service listening on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

if __name__ == "__main__":
    main()
