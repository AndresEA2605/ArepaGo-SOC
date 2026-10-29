"""PostgreSQL-backed threshold evaluation and incident lifecycle service."""
import json
import os
import uuid
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from psycopg.types.json import Jsonb
from services.common import JsonHandler
from services.db import connection, init_pool

LATENCY_THRESHOLD_MS = float(os.environ.get("LATENCY_THRESHOLD_MS", "1000"))
ERROR_RATE_THRESHOLD = float(os.environ.get("ERROR_RATE_THRESHOLD", "0.20"))
POOL_WARNING_RATIO = float(os.environ.get("POOL_WARNING_RATIO", "0.75"))
SERVICES = {"pedidos", "pagos", "calificaciones", "catalogo"}
SCHEMA = "soc_incidents"

def initialize():
    init_pool("arepago-incidents")
    with connection() as db:
        db.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        db.execute(f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.incidents (
            id UUID PRIMARY KEY,
            service TEXT NOT NULL CHECK (service IN ('pedidos','pagos','calificaciones','catalogo')),
            status TEXT NOT NULL CHECK (status IN ('open','resolved')),
            severity TEXT NOT NULL CHECK (severity IN ('warning','critical')),
            title TEXT NOT NULL,
            reasons JSONB NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            last_seen TIMESTAMPTZ NOT NULL,
            resolved_at TIMESTAMPTZ,
            latest_measurement JSONB NOT NULL
        )""")
        db.execute(f"CREATE INDEX IF NOT EXISTS incidents_status_started ON {SCHEMA}.incidents(status, started_at DESC)")
        db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS one_open_incident_per_service ON {SCHEMA}.incidents(service) WHERE status='open'")

def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def public(row):
    if row is None:
        return None
    item = dict(row)
    item["id"] = str(item["id"])
    for field in ("started_at", "last_seen", "resolved_at"):
        if item[field] is not None:
            item[field] = item[field].isoformat()
    return item

def evaluate(metric):
    service = metric.get("service")
    if service not in SERVICES:
        raise ValueError("service no válido")
    reasons = []
    latency = float(metric.get("latency_p95_ms", 0))
    if latency >= LATENCY_THRESHOLD_MS:
        reasons.append({"signal": "latency_p95_ms", "value": latency, "threshold": LATENCY_THRESHOLD_MS})
    requests, errors = int(metric.get("request_count", 0)), int(metric.get("error_count", 0))
    error_rate = errors / requests if requests else 0.0
    if requests and error_rate >= ERROR_RATE_THRESHOLD:
        reasons.append({"signal": "error_rate", "value": round(error_rate, 4), "threshold": ERROR_RATE_THRESHOLD})
    active, maximum = metric.get("db_pool_active"), metric.get("db_pool_max")
    if active is not None and maximum:
        pool_ratio = int(active) / int(maximum)
        if pool_ratio >= POOL_WARNING_RATIO:
            reasons.append({"signal": "db_pool_ratio", "value": round(pool_ratio, 4), "threshold": POOL_WARNING_RATIO})
    timestamp_text = metric.get("timestamp") or utcnow()
    timestamp = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp = timestamp.astimezone(timezone.utc)
    with connection() as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (service,))
        existing = db.execute(f"SELECT * FROM {SCHEMA}.incidents WHERE service=%s AND status='open' FOR UPDATE", (service,)).fetchone()
        if reasons:
            severity = "critical" if latency >= LATENCY_THRESHOLD_MS * 5 or error_rate >= 0.5 or (maximum and active is not None and int(active) >= int(maximum)) else "warning"
            title = f"Degradación detectada en {service}"
            if existing:
                db.execute(f"UPDATE {SCHEMA}.incidents SET severity=%s,title=%s,reasons=%s,last_seen=%s,latest_measurement=%s WHERE id=%s", (
                    severity, title, Jsonb(reasons), timestamp, Jsonb(metric), existing["id"]))
                row = db.execute(f"SELECT * FROM {SCHEMA}.incidents WHERE id=%s", (existing["id"],)).fetchone()
                return {"action": "updated", "incident": public(row)}
            incident_id = uuid.uuid4()
            db.execute(f"""INSERT INTO {SCHEMA}.incidents
                (id,service,status,severity,title,reasons,started_at,last_seen,resolved_at,latest_measurement)
                VALUES (%s,%s,'open',%s,%s,%s,%s,%s,NULL,%s)""", (
                incident_id, service, severity, title, Jsonb(reasons), timestamp, timestamp, Jsonb(metric)))
            row = db.execute(f"SELECT * FROM {SCHEMA}.incidents WHERE id=%s", (incident_id,)).fetchone()
            return {"action": "created", "incident": public(row)}
        if existing:
            db.execute(f"UPDATE {SCHEMA}.incidents SET status='resolved',resolved_at=%s,last_seen=%s,latest_measurement=%s WHERE id=%s", (
                timestamp, timestamp, Jsonb(metric), existing["id"]))
            row = db.execute(f"SELECT * FROM {SCHEMA}.incidents WHERE id=%s", (existing["id"],)).fetchone()
            return {"action": "resolved", "incident": public(row)}
    return {"action": "no_change", "incident": None}

class Handler(JsonHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            try:
                with connection() as db:
                    db.execute("SELECT 1")
                self.send_json(200, {"service": "incidents", "status": "ok", "database": "connected"})
            except Exception:
                self.send_json(503, {"service": "incidents", "status": "degraded", "database": "unavailable"})
            return
        if parsed.path == "/incidents":
            status = parse_qs(parsed.query).get("status", ["all"])[0]
            if status not in {"all", "open", "resolved"}:
                self.send_json(400, {"error": "status debe ser all, open o resolved"})
                return
            with connection() as db:
                rows = db.execute(f"SELECT * FROM {SCHEMA}.incidents WHERE status=%s ORDER BY started_at DESC LIMIT 500", (status,)).fetchall() if status != "all" else db.execute(f"SELECT * FROM {SCHEMA}.incidents ORDER BY started_at DESC LIMIT 500").fetchall()
            self.send_json(200, {"incidents": [public(row) for row in rows]})
            return
        if parsed.path.startswith("/incidents/"):
            incident_id = parsed.path.rsplit("/", 1)[-1]
            try:
                incident_id = uuid.UUID(incident_id)
            except ValueError:
                self.send_json(400, {"error": "id de incidente inválido"})
                return
            with connection() as db:
                row = db.execute(f"SELECT * FROM {SCHEMA}.incidents WHERE id=%s", (incident_id,)).fetchone()
            self.send_json(404, {"error": "incidente no encontrado"}) if row is None else self.send_json(200, public(row))
            return
        self.send_json(404, {"error": "ruta no encontrada"})

    def do_POST(self):
        if urlparse(self.path).path != "/evaluate":
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        try:
            result = evaluate(self.read_json())
        except (ValueError, TypeError, KeyError) as exc:
            self.send_json(400, {"error": f"medición inválida: {exc}"})
            return
        self.send_json(200, result)

def main():
    initialize()
    port = int(os.environ.get("PORT", "8002"))
    print(f"incidents service listening on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

if __name__ == "__main__":
    main()
