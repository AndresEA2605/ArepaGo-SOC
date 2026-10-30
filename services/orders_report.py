"""Bounded, real PostgreSQL diagnosis and report for the Orders table."""
from datetime import datetime, timezone
import time

from psycopg.errors import QueryCanceled

from services.db import autocommit_connection, connection

SCHEMA = "soc_orders"
TABLE = f"{SCHEMA}.orders"
INDEX_NAME = "orders_created_at_report"
MAX_ESTIMATED_SCAN_ROWS = 100_000
STATEMENT_TIMEOUT = "5s"
REPORT_SQL = """SELECT status, COUNT(*)::bigint AS order_count,
       COALESCE(SUM(total), 0)::numeric(14,2) AS gross_total
    FROM soc_orders.orders
    WHERE created_at >= now() - (%s * INTERVAL '1 day')
    GROUP BY status ORDER BY status"""
PLAN_SQL = "EXPLAIN (FORMAT JSON) " + REPORT_SQL


def _plan_summary(document):
    plan = document[0]["Plan"]
    nodes, scans = [], []

    def visit(item):
        node_type = item.get("Node Type", "unknown")
        nodes.append(node_type)
        if item.get("Relation Name") == "orders":
            scans.append({"node": node_type, "estimated_rows": int(item.get("Plan Rows", 0))})
        for child in item.get("Plans", []):
            visit(child)

    visit(plan)
    return {"nodes": nodes, "estimated_cost": plan.get("Total Cost"),
            "orders_scans": scans, "estimated_rows_scanned": max((s["estimated_rows"] for s in scans), default=0)}


def generate_orders_report(days=30):
    """Inspect real planner/catalog data and run a real bounded aggregate when safe."""
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
        raise ValueError("days debe ser un entero entre 1 y 365")
    report = {
        "source": "PostgreSQL real · soc_orders.orders", "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_days": days, "query": "Conteo y valor total de pedidos por estado dentro del periodo",
    }
    query_was_cancelled = False
    actual_execution_ms = None
    try:
        with connection() as db:
            relation = db.execute("SELECT to_regclass(%s) AS relation", (TABLE,)).fetchone()["relation"]
            if relation is None:
                return {**report, "status": "unavailable", "error": f"No existe la tabla {TABLE}.",
                        "recommendations": ["Inicia el servicio Pedidos y comprueba su esquema de base de datos."]}
            stats = db.execute("""SELECT GREATEST(COALESCE(s.n_live_tup, -1), c.reltuples)::bigint AS estimated_live_rows,
                    COALESCE(s.n_dead_tup, 0)::bigint AS estimated_dead_rows,
                    s.last_analyze, s.last_autoanalyze
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                LEFT JOIN pg_stat_user_tables s ON s.relid=c.oid
                WHERE n.nspname=%s AND c.relname='orders'""", (SCHEMA,)).fetchone()
            indexes = db.execute("""SELECT p.indexname, p.indexdef, i.indisvalid AS is_valid
                FROM pg_indexes p JOIN pg_class x ON x.relname=p.indexname
                JOIN pg_namespace n ON n.oid=x.relnamespace AND n.nspname=p.schemaname
                JOIN pg_index i ON i.indexrelid=x.oid
                WHERE p.schemaname=%s AND p.tablename='orders' ORDER BY p.indexname""", (SCHEMA,)).fetchall()
            plan_document = db.execute(PLAN_SQL, (days,)).fetchone()["QUERY PLAN"]
            plan = _plan_summary(plan_document)
            estimated_rows_raw = int(stats["estimated_live_rows"] if stats["estimated_live_rows"] is not None else -1)
            estimated_rows = max(0, estimated_rows_raw)
            has_time_index = any(row["is_valid"] and "(created_at" in row["indexdef"].replace(" ", "") for row in indexes)
            selective = estimated_rows > 0 and plan["estimated_rows_scanned"] / estimated_rows <= 0.20
            result_rows = None
            execution_status = "skipped_safety_limit"
            if 0 <= estimated_rows_raw and plan["estimated_rows_scanned"] <= MAX_ESTIMATED_SCAN_ROWS:
                db.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                started = time.perf_counter()
                result_rows = db.execute(REPORT_SQL, (days,)).fetchall()
                actual_execution_ms = round((time.perf_counter() - started) * 1000, 3)
                execution_status = "completed"
    except QueryCanceled:
        query_was_cancelled = True
        actual_execution_ms = 5000.0
        execution_status = "stopped_by_statement_timeout"

    recommendations = []
    if query_was_cancelled:
        recommendations.append("PostgreSQL detuvo el agregado al superar el límite de 5 segundos; revisa el plan antes de repetirlo.")
    elif execution_status == "skipped_safety_limit":
        if estimated_rows_raw < 0:
            recommendations.append("No se ejecutó el agregado porque PostgreSQL aún no tiene una estimación confiable del tamaño de la tabla.")
        else:
            recommendations.append(f"No se ejecutó el agregado: el plan estima revisar más de {MAX_ESTIMATED_SCAN_ROWS:,} filas. Se devolvió el plan y las estadísticas para evitar repetir una consulta pesada.")
    if not has_time_index and selective:
        recommendations.append("Hay un filtro de fecha selectivo y no aparece un índice por created_at. Puedes aplicar el índice concurrente desde esta pantalla y volver a comparar el plan.")
    elif plan["nodes"] and any("Seq Scan" in node for node in plan["nodes"]):
        recommendations.append("El plan actual incluye Sequential Scan. Actualizar estadísticas y comparar el plan; el optimizador puede mantenerlo si estima que resulta más barato.")
    else:
        recommendations.append("El plan no muestra Sequential Scan en la tabla de pedidos.")

    has_sequential_scan = any("Seq Scan" in node for node in plan["nodes"])
    index_recommended = (not has_time_index and estimated_rows >= 50_000 and selective)
    return {**report, "status": execution_status, "estimated_live_rows": estimated_rows,
        "estimated_dead_rows": int(stats["estimated_dead_rows"] or 0),
        "last_analyze": stats["last_analyze"].isoformat() if stats["last_analyze"] else None,
        "last_autoanalyze": stats["last_autoanalyze"].isoformat() if stats["last_autoanalyze"] else None,
        "indexes": [row["indexname"] for row in indexes], "created_at_index_present": has_time_index,
        "plan": plan,
        "actual_execution_ms": actual_execution_ms,
        "orders_by_status": ([{"status": row["status"], "orders": int(row["order_count"]),
                                "gross_total": str(row["gross_total"])} for row in result_rows]
                              if result_rows is not None else None),
        "recommendations": recommendations, "index_recommended": index_recommended,
        "analyze_recommended": has_sequential_scan,
        "safety": {"max_estimated_scan_rows": MAX_ESTIMATED_SCAN_ROWS,
                   "statement_timeout": STATEMENT_TIMEOUT, "analyze_executed": False,
                   "persistent_changes": False}}


def apply_created_at_index():
    """Create and verify a real concurrent index for the bounded date report."""
    with connection() as db:
        relation = db.execute("SELECT to_regclass(%s) AS relation", (TABLE,)).fetchone()["relation"]
        if relation is None:
            raise LookupError(f"No existe la tabla {TABLE}; no se creó ningún índice")
        existing = db.execute("""SELECT i.indisvalid AS is_valid
            FROM pg_class t JOIN pg_namespace n ON n.oid=t.relnamespace
            JOIN pg_index i ON i.indrelid=t.oid JOIN pg_class x ON x.oid=i.indexrelid
            WHERE n.nspname=%s AND t.relname='orders' AND x.relname=%s""", (SCHEMA, INDEX_NAME)).fetchone()
    if existing and existing["is_valid"]:
        return {"status": "already_exists", "index": INDEX_NAME,
                "message": "El índice ya existía; no se cambió el esquema."}
    with autocommit_connection() as db:
        if existing:
            db.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {SCHEMA}.{INDEX_NAME}")
        db.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} ON {TABLE} (created_at)")
    with connection() as db:
        verified = db.execute("""SELECT x.relname AS indexname, pg_get_indexdef(x.oid) AS indexdef,
                i.indisvalid AS is_valid
            FROM pg_class t JOIN pg_namespace n ON n.oid=t.relnamespace
            JOIN pg_index i ON i.indrelid=t.oid JOIN pg_class x ON x.oid=i.indexrelid
            WHERE n.nspname=%s AND t.relname='orders' AND x.relname=%s""", (SCHEMA, INDEX_NAME)).fetchone()
    if not verified or not verified["is_valid"]:
        raise RuntimeError("PostgreSQL terminó la operación, pero el índice no aparece en pg_indexes")
    return {"status": "created", "index": verified["indexname"], "definition": verified["indexdef"],
            "message": "Índice creado concurrentemente y verificado en soc_orders.orders."}
