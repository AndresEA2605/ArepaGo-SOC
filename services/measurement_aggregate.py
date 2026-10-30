"""Pure aggregation of per-replica telemetry into one sample per service.

The gateway samples run independently in every router replica, so a single
interval produces one row per replica. Incident evaluation needs one verdict
per service, not one per replica: counts add up, the worst latency/pool wins
and diagnostics are merged.
"""
from datetime import datetime, timezone


def _as_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def merge_diagnostics(diagnostics):
    """Merge route diagnostics from several replicas, keeping the worst 20."""
    merged = {}
    for item in diagnostics or []:
        if not isinstance(item, dict):
            continue
        method = str(item.get("method") or "GET")[:8]
        route = str(item.get("route") or "/")[:160]
        key = (method, route)
        entry = merged.setdefault(key, {"method": method, "route": route,
                                        "requests": 0, "errors": 0, "statuses": {}})
        try:
            entry["requests"] += int(item.get("requests") or 0)
            entry["errors"] += int(item.get("errors") or 0)
        except (TypeError, ValueError):
            pass
        statuses = item.get("statuses") or {}
        if isinstance(statuses, dict):
            for status, count in statuses.items():
                try:
                    entry["statuses"][str(status)] = entry["statuses"].get(str(status), 0) + int(count or 0)
                except (TypeError, ValueError):
                    continue
    return sorted(merged.values(), key=lambda item: (item["errors"], item["requests"]),
                  reverse=True)[:20]


def aggregate_measurements(rows):
    """Combine the latest measurement of each replica into one service sample.

    ``rows`` must already be the freshest row per replica. Returns ``None`` when
    there is nothing to aggregate.
    """
    rows = [dict(row) for row in rows if row]
    if not rows:
        return None
    request_count = sum(int(row.get("request_count") or 0) for row in rows)
    error_count = sum(int(row.get("error_count") or 0) for row in rows)
    latency = max(float(row.get("latency_p95_ms") or 0) for row in rows)
    pools = [(int(row["db_pool_active"]), int(row["db_pool_max"])) for row in rows
             if row.get("db_pool_active") is not None and row.get("db_pool_max")]
    worst_pool = max(pools, key=lambda pair: pair[0] / pair[1]) if pools else None
    timestamps = [_as_datetime(row["timestamp"]) for row in rows if row.get("timestamp")]
    replicas = sorted({str(row["replica"]) for row in rows if row.get("replica")})
    measurement_ids = sorted(str(row["id"]) for row in rows if row.get("id"))
    diagnostics = merge_diagnostics(
        [item for row in rows for item in (row.get("diagnostics") or [])])
    return {
        "service": rows[0].get("service"),
        "timestamp": max(timestamps).isoformat(timespec="seconds") if timestamps else None,
        "environment": rows[0].get("environment"),
        "latency_p95_ms": round(latency, 3),
        "request_count": request_count,
        "error_count": error_count,
        "db_pool_active": worst_pool[0] if worst_pool else None,
        "db_pool_max": worst_pool[1] if worst_pool else None,
        "source": f"aggregated latest sample from {len(rows)} replica(s)",
        "diagnostics": diagnostics,
        "replica_count": len(rows),
        "replicas": replicas,
        "measurement_ids": measurement_ids,
        "sample_key": "|".join(measurement_ids),
    }
