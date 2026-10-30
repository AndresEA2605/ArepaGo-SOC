"""PostgreSQL-backed threshold evaluation and incident lifecycle service."""
import json
import ast
import os
import uuid
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from psycopg.types.json import Jsonb
from services.common import JsonHandler, log_event
from services.db import connection, init_pool, pool_stats
from services.incident_rules import recovery_decision
from services.internal_auth import IDENTITY_HEADER, load_secret, verify_identity
from services.incident_simulator import (benchmark_rows, reference_scenario, run_local_benchmark,
                                         simulate_mitigations)
from services.orders_report import apply_created_at_index, generate_orders_report

LATENCY_THRESHOLD_MS = float(os.environ.get("LATENCY_THRESHOLD_MS", "1000"))
ERROR_RATE_THRESHOLD = float(os.environ.get("ERROR_RATE_THRESHOLD", "0.20"))
POOL_WARNING_RATIO = float(os.environ.get("POOL_WARNING_RATIO", "0.75"))
RECOVERY_SAMPLES_REQUIRED = max(2, min(10, int(os.environ.get("INCIDENT_RECOVERY_SAMPLES", "3"))))
# Un incidente sin tráfico no puede validarse con muestras; tras esta ventana se
# cierra por inactividad para no dejarlo abierto indefinidamente. 0 lo desactiva.
INACTIVITY_MINUTES = max(0, min(1440, int(os.environ.get("INCIDENT_INACTIVITY_MINUTES", "30"))))
INACTIVITY_SECONDS = INACTIVITY_MINUTES * 60
SERVICES = {"pedidos", "pagos", "calificaciones", "catalogo"}
SCHEMA = "soc_incidents"


def current_user(handler):
    return verify_identity(handler.headers.get(IDENTITY_HEADER, ""))


def _source_method(filename, symbol):
    source_path = Path(__file__).with_name(filename)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for parent in ast.walk(tree):
        if isinstance(parent, ast.ClassDef) and parent.name == "Handler":
            for item in parent.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == symbol:
                    lines = source.splitlines()
                    start, end = item.lineno, getattr(item, "end_lineno", item.lineno)
                    return {"path": f"services/{filename}", "symbol": symbol, "start_line": start,
                            "end_line": end, "source": "\n".join(lines[start - 1:end])}
    return None


def incident_code_context(incident):
    service = incident["service"]
    measurement = incident.get("initial_measurement") or incident.get("latest_measurement") or {}
    diagnostics = [item for item in measurement.get("diagnostics", []) if item.get("errors", 0) > 0]
    symbols = []
    for item in diagnostics:
        method = str(item.get("method", "GET")).upper()
        route = str(item.get("route", ""))
        domain_path = route.removeprefix(f"/api/{service}") or "/"
        if domain_path.startswith("/health"):
            symbols.append(("business.py", "do_GET"))
        elif service == "pedidos":
            symbols.append(("business.py", {"GET": "orders_get", "POST": "orders_post", "PATCH": "order_cancel" if domain_path.endswith("/cancel") else "order_status_update"}.get(method, "do_GET")))
        elif service == "pagos":
            symbols.append(("business.py", {"GET": "payments_get", "POST": "payments_post", "PATCH": "payment_cancel" if domain_path.endswith("/cancel") else "payment_confirm"}.get(method, "do_GET")))
        elif service == "catalogo":
            symbols.append(("business.py", {"GET": "catalog_get", "POST": "catalog_post", "PATCH": "catalog_update", "PUT": "catalog_update", "DELETE": "catalog_delete"}.get(method, "do_GET")))
        elif service == "calificaciones":
            symbols.append(("business.py", {"GET": "ratings_get", "POST": "ratings_post"}.get(method, "do_GET")))
    snippets = [_source_method("gateway.py", "route_request"), _source_method("gateway.py", "proxy")]
    for filename, symbol in symbols:
        snippets.append(_source_method(filename, symbol))
    unique = []
    seen = set()
    for snippet in snippets:
        if snippet and (snippet["path"], snippet["symbol"]) not in seen:
            seen.add((snippet["path"], snippet["symbol"]))
            unique.append(snippet)
        if len(unique) >= 5:
            break
    statuses = {status for item in diagnostics for status, count in (item.get("statuses") or {}).items()
                if int(count) and int(status) >= 500}
    explanation = ("El gateway registró un error del upstream; con HTTP 502/503 la petición puede no haber alcanzado el handler de negocio."
                   if statuses & {"502", "503", "504"} else
                   "Fragmentos de las rutas de gateway y negocio que corresponden al método y endpoint con errores.")
    return {"source_revision": "Código empaquetado en la imagen actual del servicio Incident; puede diferir si el servicio afectado corre otra revisión.",
            "explanation": explanation, "snippets": unique}


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
            latest_measurement JSONB NOT NULL,
            initial_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
            initial_measurement JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            recovery_count INTEGER NOT NULL DEFAULT 0 CHECK (recovery_count >= 0),
            recovery_required INTEGER NOT NULL DEFAULT {RECOVERY_SAMPLES_REQUIRED} CHECK (recovery_required >= 2),
            resolution_reason TEXT NOT NULL DEFAULT ''
        )""")
        db.execute(f"ALTER TABLE {SCHEMA}.incidents ADD COLUMN IF NOT EXISTS initial_reasons JSONB NOT NULL DEFAULT '[]'::jsonb")
        db.execute(f"ALTER TABLE {SCHEMA}.incidents ADD COLUMN IF NOT EXISTS initial_measurement JSONB NOT NULL DEFAULT '{{}}'::jsonb")
        db.execute(f"ALTER TABLE {SCHEMA}.incidents ADD COLUMN IF NOT EXISTS recovery_count INTEGER NOT NULL DEFAULT 0")
        db.execute(f"ALTER TABLE {SCHEMA}.incidents ADD COLUMN IF NOT EXISTS recovery_required INTEGER NOT NULL DEFAULT {RECOVERY_SAMPLES_REQUIRED}")
        db.execute(f"ALTER TABLE {SCHEMA}.incidents ADD COLUMN IF NOT EXISTS resolution_reason TEXT NOT NULL DEFAULT ''")
        # Preserve the best available opening evidence for incidents created before these columns existed.
        db.execute(f"""UPDATE {SCHEMA}.incidents SET
            initial_reasons = CASE WHEN initial_reasons = '[]'::jsonb THEN reasons ELSE initial_reasons END,
            initial_measurement = CASE WHEN initial_measurement = '{{}}'::jsonb THEN latest_measurement ELSE initial_measurement END
            WHERE initial_reasons = '[]'::jsonb OR initial_measurement = '{{}}'::jsonb""")
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
                db.execute(f"""UPDATE {SCHEMA}.incidents SET severity=%s,title=%s,reasons=%s,last_seen=%s,
                    latest_measurement=%s,recovery_count=0 WHERE id=%s""",
                    (severity, title, Jsonb(reasons), timestamp, Jsonb(metric), existing["id"]))
                row = db.execute(f"SELECT * FROM {SCHEMA}.incidents WHERE id=%s", (existing["id"],)).fetchone()
                return {"action": "updated", "incident": public(row)}
            incident_id = uuid.uuid4()
            db.execute(f"""INSERT INTO {SCHEMA}.incidents
                (id,service,status,severity,title,reasons,started_at,last_seen,resolved_at,latest_measurement,
                 initial_reasons,initial_measurement,recovery_count,recovery_required)
                VALUES (%s,%s,'open',%s,%s,%s,%s,%s,NULL,%s,%s,%s,0,%s)""", (
                incident_id, service, severity, title, Jsonb(reasons), timestamp, timestamp, Jsonb(metric),
                Jsonb(reasons), Jsonb(metric), RECOVERY_SAMPLES_REQUIRED))
            row = db.execute(f"SELECT * FROM {SCHEMA}.incidents WHERE id=%s", (incident_id,)).fetchone()
            return {"action": "created", "incident": public(row)}
        if existing:
            seconds_since_breach = max(0.0, (timestamp - existing["last_seen"]).total_seconds())
            action, recovery_count, resolution_reason = recovery_decision(
                requests, int(existing.get("recovery_count", 0)),
                int(existing.get("recovery_required", RECOVERY_SAMPLES_REQUIRED)),
                seconds_since_breach, INACTIVITY_SECONDS)
            if action == "awaiting_traffic":
                # A quiet window proves nothing either way: keep the accumulated
                # evidence instead of resetting it, so intermittent traffic can
                # still confirm recovery.
                db.execute(f"""UPDATE {SCHEMA}.incidents SET latest_measurement=%s
                    WHERE id=%s""", (Jsonb(metric), existing["id"]))
            elif action == "recovering":
                db.execute(f"""UPDATE {SCHEMA}.incidents SET latest_measurement=%s,recovery_count=%s
                    WHERE id=%s""", (Jsonb(metric), recovery_count, existing["id"]))
            else:
                db.execute(f"""UPDATE {SCHEMA}.incidents SET status='resolved',resolved_at=%s,last_seen=%s,
                    latest_measurement=%s,recovery_count=%s,resolution_reason=%s WHERE id=%s""",
                    (timestamp, timestamp, Jsonb(metric), recovery_count, resolution_reason, existing["id"]))
                log_event("incident_resolved", level="info", service=service, incident_id=str(existing["id"]),
                          reason=resolution_reason, recovery_count=recovery_count)
            row = db.execute(f"SELECT * FROM {SCHEMA}.incidents WHERE id=%s", (existing["id"],)).fetchone()
            return {"action": action, "incident": public(row)}
    return {"action": "no_change", "incident": None}

class Handler(JsonHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/health", "/health/detailed"}:
            try:
                with connection() as db:
                    db.execute("SELECT 1")
                details = {"service": "incidents", "status": "ok", "database": "connected"}
                if parsed.path == "/health/detailed": details["database_pool"] = pool_stats()
                self.send_json(200, details)
            except Exception as exc:
                self.log_exception(exc, "health_check_failed")
                self.send_json(503, {"service": "incidents", "status": "degraded", "database": "unavailable",
                                     "diagnostic": type(exc).__name__, "database_pool": pool_stats()})
            return
        if parsed.path == "/incidents/simulator/reference":
            self.send_json(200, reference_scenario())
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
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "incidents" and parts[2] == "code-context":
            try:
                incident_id = uuid.UUID(parts[1])
            except ValueError:
                self.send_json(400, {"error": "id de incidente inválido"})
                return
            with connection() as db:
                row = db.execute(f"SELECT * FROM {SCHEMA}.incidents WHERE id=%s", (incident_id,)).fetchone()
            if row is None:
                self.send_json(404, {"error": "incidente no encontrado"})
                return
            self.send_json(200, incident_code_context(dict(row)))
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
        path = urlparse(self.path).path
        if path.startswith("/actions/") or path == "/incidents/simulator/run":
            user = current_user(self)
            if user is None or user["role"] != "admin":
                self.send_json(403, {"error": "se requiere rol administrador"})
                return
        if path == "/actions/orders-report":
            try:
                payload = self.read_json()
                if not isinstance(payload, dict):
                    self.send_json(400, {"error": "el cuerpo debe ser un objeto JSON"})
                    return
                result = generate_orders_report(payload.get("days", 30))
                self.send_json(200, result)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception as exc:
                self.log_exception(exc, "orders_report_failed")
                self.send_json(503, {"error": "no se pudo generar el reporte real de pedidos",
                                     "diagnostic": type(exc).__name__})
            return
        if path == "/actions/index-orders-created-at":
            try:
                current_report = generate_orders_report(30)
                if not current_report.get("index_recommended") and not current_report.get("created_at_index_present"):
                    self.send_json(409, {"error": "el diagnóstico real no recomienda crear el índice; no se aplicó ningún cambio",
                                         "report": current_report})
                    return
                result = apply_created_at_index()
                log_event("remediation_applied", service="pedidos", action="create_orders_created_at_index",
                          actor=user["id"], index=result["index"])
                try:
                    result["report"] = generate_orders_report(30)
                except Exception as exc:
                    self.log_exception(exc, "orders_report_after_index_failed")
                    result["report_error"] = "El índice se creó y verificó; no se pudo volver a generar el reporte. Intenta actualizarlo."
                self.send_json(200, result)
            except LookupError as exc:
                self.send_json(409, {"error": str(exc)})
            except Exception as exc:
                self.log_exception(exc, "orders_index_remediation_failed")
                self.send_json(503, {"error": "no se pudo crear y verificar el índice concurrente",
                                     "diagnostic": type(exc).__name__})
            return
        if path == "/actions/analyze-orders":
            try:
                with connection() as db:
                    relation = db.execute("SELECT to_regclass('soc_orders.orders') AS relation").fetchone()["relation"]
                    if relation is None:
                        self.send_json(409, {"error": "no existe soc_orders.orders; no se ejecutó ninguna acción"})
                        return
                    db.execute("ANALYZE soc_orders.orders")
                    stats = db.execute("""SELECT n_live_tup, n_dead_tup, last_analyze, last_autoanalyze
                        FROM pg_stat_user_tables WHERE schemaname='soc_orders' AND relname='orders'""").fetchone()
                result = {"action": "analyze_orders", "status": "completed", "table": "soc_orders.orders",
                    "statistics": {"estimated_live_rows": stats["n_live_tup"], "estimated_dead_rows": stats["n_dead_tup"],
                        "last_analyze": stats["last_analyze"].isoformat() if stats["last_analyze"] else None,
                        "last_autoanalyze": stats["last_autoanalyze"].isoformat() if stats["last_autoanalyze"] else None},
                    "effect": "actualizó estadísticas del planificador PostgreSQL; no cambió filas ni creó índices",
                    "limitation": "solo ayuda si las estadísticas estaban desactualizadas; verificar la consulta real con EXPLAIN (ANALYZE, BUFFERS)"}
                log_event("remediation_applied", service="pedidos", action="analyze_orders",
                          actor=user["id"], table="soc_orders.orders")
                self.send_json(200, result)
            except Exception as exc:
                self.log_exception(exc, "remediation_failed")
                self.send_json(503, {"error": "no se pudieron actualizar las estadísticas de Pedidos",
                                     "diagnostic": type(exc).__name__})
            return
        if urlparse(self.path).path == "/incidents/simulator/run":
            try:
                payload = self.read_json()
                result = simulate_mitigations(payload)
                if payload.get("mode") == "local_benchmark":
                    rows = benchmark_rows(payload.get("rows", 100_000))
                    pool_before = pool_stats()
                    with connection() as db:
                        measured = run_local_benchmark(db, rows, apply_index="index" in payload["mitigations"])
                    measured["pool_before"] = pool_before
                    measured["pool_after"] = pool_stats()
                    result["execution_mode"] = "local_postgresql_benchmark"
                    result["actual_measurements"] = measured
                    result["benchmark_timestamp"] = utcnow()
                    result["notice"] = ("EXPLAIN ANALYZE se ejecutó contra datos sintéticos en una tabla temporal. "
                        "Los tiempos y planes son mediciones reales de esta instancia local, no del incidente histórico. "
                        "La tabla se elimina al finalizar la transacción; no se cambian tablas de negocio ni se fuerza "
                        "la saturación de Pedidos/Pagos.")
                    result["replica_status"] = "no configurada; para probarla se requiere una tabla de benchmark ya disponible en réplica"
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            except Exception as exc:
                self.log_exception(exc, "local_benchmark_failed")
                self.send_json(503, {"error": "falló el benchmark PostgreSQL local", "diagnostic": type(exc).__name__})
                return
            self.send_json(200, result)
            return
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
    load_secret()
    initialize()
    port = int(os.environ.get("PORT", "8002"))
    print(f"incidents service listening on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

if __name__ == "__main__":
    main()
