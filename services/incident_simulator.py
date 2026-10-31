"""Reference scenario and bounded, isolated PostgreSQL benchmark helpers."""
from datetime import datetime, timedelta

SIMULATOR_MITIGATIONS = {"reschedule", "index", "read_replica"}


def reference_scenario():
    return {"scenario_id": "arepago-2026-09-27-report-overload", "simulation_only": True,
        "persisted": False, "date": "2026-09-27", "time_window": {"start": "08:03", "end": "08:22",
        "duration_minutes": 19, "timezone": "America/Bogota (UTC-5)"},
        "peak_window": {"start": "08:12", "end": "08:14", "sample_requests": 600,
                        "pedidos_failed": 384, "pedidos_failure_rate": 64,
                        "pagos_failed": 354, "pagos_failure_rate": 59},
        "economic_impact_cop": 5182760000.0,
        "affected_customers": 103800,
        "social_media_mentions": 34200,
        "report": {"orders_examined": 14200000, "query_plan": "Sequential Scan",
                   "query_text_available": False, "index_candidate_assumption": "created_at date-range predicate"},
        "cause_summary": "El reporte pesado recorrió secuencialmente ~14,2 millones de pedidos y coincidió con la saturación de los pools de Pedidos y Pagos, configurados en 20 conexiones cada uno.",
        "failed_operations": "Operaciones de Pedidos y Pagos; el detalle de excepciones no está disponible en la referencia.",
        "connection_pools": {"pedidos": {"configured": 20, "state": "saturated"},
                              "pagos": {"configured": 20, "state": "saturated"}},
        "modules": {"pedidos": {"failure_rate_percent": 64, "impact": "affected"},
                    "pagos": {"failure_rate_percent": 59, "impact": "affected"},
                    "calificaciones": {"failure_rate_percent": 0, "impact": "not affected"},
                    "catalogo": {"failure_rate_percent": 0, "impact": "not affected"}},
        "source_classification": "user-provided reference values; not production telemetry"}


def simulate_mitigations(data):
    selected = data.get("mitigations")
    if not isinstance(selected, list) or not 1 <= len(selected) <= len(SIMULATOR_MITIGATIONS) or any(
            not isinstance(item, str) or item not in SIMULATOR_MITIGATIONS for item in selected):
        raise ValueError("mitigations debe incluir al menos una opción válida: reschedule, index o read_replica")
    selected = list(dict.fromkeys(selected))
    before = reference_scenario()
    details = {
        "reschedule": {"name": "Mover el reporte fuera del horario pico",
            "mechanism": "Se elimina el solapamiento del reporte con el intervalo crítico 08:03–08:22.",
            "after": {"business_window_overlap": False, "query_plan": "Sequential Scan (sin cambio)",
                "primary_pools_during_reference_window": "el reporte ya no consume conexiones en ese intervalo",
                "incident_attributable_failures_during_window": "no modeladas; tasa base del sistema desconocida"},
            "assumptions": ["El nuevo horario no coincide con otro pico", "El reporte se conserva y aún puede escanear 14,2 millones de filas"]},
        "index": {"name": "Evaluar un índice para la consulta del reporte",
            "mechanism": "Un índice puede evitar el Sequential Scan si coincide con los filtros y la selectividad de la consulta.",
            "after": {"business_window_overlap": True, "query_plan": "Index Scan esperado, condicionado a EXPLAIN ANALYZE",
                "primary_pools": "posible reducción de duración y uso; no cuantificable con los datos disponibles",
                "incident_attributable_failures": "no cuantificadas"},
            "assumptions": ["Se supone un filtro por rango de created_at; la consulta real no fue proporcionada",
                "DDL candidato, no ejecutado: CREATE INDEX CONCURRENTLY idx_orders_created_at ON soc_orders.orders (created_at)",
                "El plan debe confirmarse con EXPLAIN (ANALYZE, BUFFERS); un índice no garantiza mejora"]},
        "read_replica": {"name": "Ejecutar el reporte en una réplica de solo lectura",
            "mechanism": "Separa la consulta analítica del PostgreSQL primario que atiende Pedidos y Pagos.",
            "after": {"business_window_overlap": True, "query_plan": "Sequential Scan posible en la réplica",
                "primary_pools": "este reporte deja de consumir los pools primarios de Pedidos y Pagos",
                "incident_attributable_failures": "la cascada por saturación primaria se evita en el modelo; tasa residual desconocida"},
            "assumptions": ["La réplica tiene capacidad independiente suficiente", "Se acepta y supervisa el retraso de replicación"]},
    }
    return {"simulation_only": True, "persisted": False, "scenario": before,
        "mitigations": [{"id": item, **details[item], "result_classification": "qualitative simulation inference; not measured"}
                        for item in selected],
        "notice": "La simulación no ejecuta SQL, no modifica datos ni pools y no agrega mediciones a Telemetry.",
        "baseline_source": before["source_classification"]}


def benchmark_rows(value, maximum=500_000):
    """Validate benchmark size; local synthetic data is intentionally bounded."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1_000 <= value <= maximum:
        raise ValueError(f"rows debe ser un entero entre 1000 y {maximum}")
    return value


def explain_plan(db, query, params):
    row = db.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params).fetchone()
    document = next(iter(row.values()))
    plan = document[0]
    nodes = []

    def visit(node):
        nodes.append(node.get("Node Type", "unknown"))
        for child in node.get("Plans", []):
            visit(child)

    visit(plan["Plan"])
    return {"execution_time_ms": plan.get("Execution Time"), "planning_time_ms": plan.get("Planning Time"),
            "plan_nodes": nodes, "shared_hit_blocks": plan["Plan"].get("Shared Hit Blocks", 0),
            "shared_read_blocks": plan["Plan"].get("Shared Read Blocks", 0)}


def run_local_benchmark(db, rows, apply_index=True):
    """Measure real SQL against a temporary table; no business rows are touched."""
    rows = benchmark_rows(rows)
    table = "arepago_sim_orders"
    query = f"SELECT count(*) FROM {table} WHERE created_at >= %s"
    db.execute(f"CREATE TEMP TABLE {table} ON COMMIT DROP AS "
               "SELECT n::bigint AS id, (timestamp '2026-01-01' + n * interval '1 second') AS created_at "
               "FROM generate_series(1, %s) AS n", (rows,))
    db.execute(f"ANALYZE {table}")
    cutoff = datetime(2026, 1, 1) + timedelta(seconds=rows - max(100, rows // 100))
    before = explain_plan(db, query, (cutoff,))
    after = None
    if apply_index:
        db.execute(f"CREATE INDEX arepago_sim_orders_created_at_idx ON {table}(created_at)")
        db.execute(f"ANALYZE {table}")
        after = explain_plan(db, query, (cutoff,))
    return {"rows_generated": rows, "query": "SELECT count(*) ... WHERE created_at >= $1",
            "before_index": before, "after_index": after,
            "execution_time_change_percent": (round((after["execution_time_ms"] - before["execution_time_ms"]) /
                before["execution_time_ms"] * 100, 2) if after and before["execution_time_ms"] else None),
            "storage": "temporary PostgreSQL table; dropped when the connection transaction ends",
            "pool_scope": "incident-service connection pool; does not saturate Pedidos/Pagos pools"}
