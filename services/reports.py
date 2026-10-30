"""Persistent customer reports about the website and ArepaGo storefront."""
import os
import uuid
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from services.common import JsonHandler, log_event
from services.db import connection, init_pool, pool_stats
from services.internal_auth import IDENTITY_HEADER, load_secret, verify_identity
from services.migrations import apply_migrations
from services.report_rules import REPORT_CATEGORIES, REPORT_STATUSES, can_transition_report, validate_report

SCHEMA = "soc_reports"


MIGRATIONS = [
    (1, "reports and audit events", [
        f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.reports (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(), customer_id UUID NOT NULL,
            category TEXT NOT NULL CHECK (category IN ('web_issue','store_complaint')),
            subject TEXT NOT NULL CHECK (length(subject) BETWEEN 5 AND 160),
            message TEXT NOT NULL CHECK (length(message) BETWEEN 15 AND 4000), order_id UUID,
            status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','in_review','resolved','closed')),
            admin_response TEXT NOT NULL DEFAULT '' CHECK (length(admin_response) <= 4000),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        f"CREATE INDEX IF NOT EXISTS reports_created ON {SCHEMA}.reports(created_at DESC)",
        f"CREATE INDEX IF NOT EXISTS reports_customer_created ON {SCHEMA}.reports(customer_id,created_at DESC)",
        f"CREATE INDEX IF NOT EXISTS reports_status_created ON {SCHEMA}.reports(status,created_at DESC)",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.report_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(), report_id UUID NOT NULL REFERENCES {SCHEMA}.reports(id) ON DELETE CASCADE,
            actor_id UUID NOT NULL, previous_status TEXT, new_status TEXT NOT NULL, response TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        f"CREATE INDEX IF NOT EXISTS report_events_created ON {SCHEMA}.report_events(report_id,created_at)",
    ]),
]


def initialize():
    init_pool("arepago-reports")
    with connection() as db:
        apply_migrations(db, "reports", MIGRATIONS)


def current_user(handler):
    return verify_identity(handler.headers.get(IDENTITY_HEADER, ""))


def public_report(row, events=None):
    result = {key: row[key] for key in ("id", "customer_id", "category", "subject", "message", "order_id", "status",
        "admin_response", "created_at", "updated_at")}
    for key in ("id", "customer_id", "order_id"):
        if result[key] is not None:
            result[key] = str(result[key])
    for key in ("created_at", "updated_at"):
        result[key] = result[key].isoformat()
    if events is not None:
        result["history"] = [{"actor_id": str(event["actor_id"]), "previous_status": event["previous_status"],
            "new_status": event["new_status"], "response": event["response"], "created_at": event["created_at"].isoformat()}
            for event in events]
    return result


class Handler(JsonHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/health", "/health/detailed"}:
            try:
                with connection() as db:
                    db.execute("SELECT 1")
                payload = {"service": "reports", "status": "ok", "database": "connected"}
                if parsed.path == "/health/detailed":
                    payload["database_pool"] = pool_stats()
                self.send_json(200, payload)
            except Exception as exc:
                self.log_exception(exc, "health_check_failed")
                self.send_json(503, {"service": "reports", "status": "degraded", "database": "unavailable",
                                     "diagnostic": type(exc).__name__})
            return
        user = current_user(self)
        if user is None:
            self.send_json(401, {"error": "identidad interna inválida"})
            return
        path = parsed.path.rstrip("/") or "/"
        if path == "/reports":
            filters = parse_qs(parsed.query)
            status, category = filters.get("status", ["all"])[0], filters.get("category", ["all"])[0]
            if status not in REPORT_STATUSES | {"all"} or category not in REPORT_CATEGORIES | {"all"}:
                self.send_json(400, {"error": "filtro de reportes inválido"})
                return
            with connection() as db:
                conditions, params = [], []
                if user["role"] != "admin":
                    conditions.append("customer_id=%s"); params.append(user["id"])
                if status != "all":
                    conditions.append("status=%s"); params.append(status)
                if category != "all":
                    conditions.append("category=%s"); params.append(category)
                where = " WHERE " + " AND ".join(conditions) if conditions else ""
                rows = db.execute(f"SELECT * FROM {SCHEMA}.reports{where} ORDER BY created_at DESC LIMIT 200", params).fetchall()
                events_by_report = {}
                if user["role"] == "admin" and rows:
                    event_rows = db.execute(f"SELECT report_id,actor_id,previous_status,new_status,response,created_at FROM {SCHEMA}.report_events WHERE report_id=ANY(%s) ORDER BY created_at", ([row["id"] for row in rows],)).fetchall()
                    for event in event_rows:
                        events_by_report.setdefault(event["report_id"], []).append(event)
                reports = [public_report(row, events_by_report.get(row["id"], []) if user["role"] == "admin" else None) for row in rows]
            self.send_json(200, {"reports": reports})
            return
        parts = path.split("/")
        if len(parts) == 3 and parts[1] == "reports":
            try:
                report_id = uuid.UUID(parts[2])
            except ValueError:
                self.send_json(400, {"error": "id de reporte inválido"})
                return
            with connection() as db:
                if user["role"] == "admin":
                    row = db.execute(f"SELECT * FROM {SCHEMA}.reports WHERE id=%s", (report_id,)).fetchone()
                    events = db.execute(f"SELECT actor_id,previous_status,new_status,response,created_at FROM {SCHEMA}.report_events WHERE report_id=%s ORDER BY created_at", (report_id,)).fetchall() if row else None
                else:
                    row = db.execute(f"SELECT * FROM {SCHEMA}.reports WHERE id=%s AND customer_id=%s", (report_id,user["id"])).fetchone()
                    events = None
            self.send_json(404, {"error": "reporte no encontrado"}) if row is None else self.send_json(200, {"report": public_report(row, events)})
            return
        self.send_json(404, {"error": "ruta no encontrada"})

    def do_POST(self):
        if urlparse(self.path).path.rstrip("/") != "/reports":
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        user = current_user(self)
        if user is None or user["role"] != "customer":
            self.send_json(403, {"error": "solo un cliente autenticado puede enviar reportes"})
            return
        try:
            report = validate_report(self.read_json())
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        with connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (user["id"],))
            if report["order_id"]:
                order = db.execute("SELECT id FROM soc_orders.orders WHERE id=%s AND customer_id=%s", (report["order_id"],user["id"])).fetchone()
                if order is None:
                    self.send_json(404, {"error": "pedido asociado no encontrado"})
                    return
            recent = db.execute(f"SELECT COUNT(*) AS total FROM {SCHEMA}.reports WHERE customer_id=%s AND created_at>now()-interval '1 hour'", (user["id"],)).fetchone()["total"]
            if recent >= 10:
                self.send_json(429, {"error": "límite alcanzado: máximo 10 reportes por hora"})
                return
            row = db.execute(f"""INSERT INTO {SCHEMA}.reports(customer_id,category,subject,message,order_id)
                VALUES (%s,%s,%s,%s,%s) RETURNING *""", (user["id"],report["category"],report["subject"],report["message"],report["order_id"])).fetchone()
            db.execute(f"INSERT INTO {SCHEMA}.report_events(report_id,actor_id,new_status) VALUES (%s,%s,'new')", (row["id"],user["id"]))
        log_event("customer_report_created", service="reports", report_id=str(row["id"]), category=row["category"])
        self.send_json(201, {"report": public_report(row)})

    def do_PATCH(self):
        path = urlparse(self.path).path.rstrip("/")
        parts = path.split("/")
        user = current_user(self)
        if user is None or user["role"] != "admin":
            self.send_json(403, {"error": "se requiere rol administrador"})
            return
        if len(parts) != 3 or parts[1] != "reports":
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        try:
            report_id, data = uuid.UUID(parts[2]), self.read_json()
        except (ValueError, TypeError) as exc:
            self.send_json(400, {"error": "id o cuerpo de reporte inválido"})
            return
        target, response = data.get("status"), data.get("admin_response", "")
        if not isinstance(target, str) or target not in REPORT_STATUSES or not isinstance(response, str) or len(response) > 4000:
            self.send_json(400, {"error": "estado o respuesta administrativa inválidos"})
            return
        with connection() as db:
            row = db.execute(f"SELECT * FROM {SCHEMA}.reports WHERE id=%s FOR UPDATE", (report_id,)).fetchone()
            if row is None:
                self.send_json(404, {"error": "reporte no encontrado"})
                return
            response_changed = bool(response.strip()) and response.strip() != row["admin_response"]
            if target == row["status"] and not response_changed:
                self.send_json(400, {"error": "cambia el estado o agrega una respuesta para guardar seguimiento"})
                return
            if target != row["status"] and not can_transition_report(row["status"], target):
                self.send_json(409, {"error": f"transición no permitida: {row['status']} → {target}"})
                return
            response = response.strip() or row["admin_response"]
            if target in {"resolved", "closed"} and len(response) < 3:
                self.send_json(400, {"error": "incluye una respuesta antes de resolver o cerrar el reporte"})
                return
            updated = db.execute(f"UPDATE {SCHEMA}.reports SET status=%s,admin_response=%s,updated_at=now() WHERE id=%s RETURNING *", (target,response,report_id)).fetchone()
            db.execute(f"""INSERT INTO {SCHEMA}.report_events(report_id,actor_id,previous_status,new_status,response)
                VALUES (%s,%s,%s,%s,%s)""", (report_id,user["id"],row["status"],target,response))
        log_event("customer_report_updated", service="reports", report_id=str(report_id), actor=user["id"], status=target)
        self.send_json(200, {"report": public_report(updated)})


def main():
    load_secret()
    initialize()
    port = int(os.environ.get("PORT", "8005"))
    print(f"reports service listening on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
