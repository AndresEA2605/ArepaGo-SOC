#!/usr/bin/env python3
"""Seed telemetry measurements from the 2026-09-27 incident report (08:00 - 08:22).

Inserts real historical telemetry into the SOC via the gateway API, matching the
timeline, latency peaks, error rates, and connection pool saturation documented in
the report and Annexes B and C for the regulatory body (Ente de Control).
"""
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = "http://localhost:8081"

# Measurements corresponding to the 10 key timeline moments from the regulatory report
INCIDENT_TIMELINE = [
    # 07:55 - Línea base normal
    {"timestamp": "2026-09-27T07:55:00Z", "service": "pedidos", "latency_p95_ms": 120, "request_count": 200, "error_count": 0, "db_pool_active": 2, "db_pool_max": 20},
    {"timestamp": "2026-09-27T07:55:00Z", "service": "pagos", "latency_p95_ms": 110, "request_count": 200, "error_count": 0, "db_pool_active": 2, "db_pool_max": 20},
    {"timestamp": "2026-09-27T07:55:00Z", "service": "catalogo", "latency_p95_ms": 50, "request_count": 300, "error_count": 0, "db_pool_active": 3, "db_pool_max": 20},
    {"timestamp": "2026-09-27T07:55:00Z", "service": "calificaciones", "latency_p95_ms": 60, "request_count": 150, "error_count": 0, "db_pool_active": 4, "db_pool_max": 20},

    # 08:00 - Inicia el reporte automático de ventas
    {"timestamp": "2026-09-27T08:00:00Z", "service": "pedidos", "latency_p95_ms": 150, "request_count": 250, "error_count": 0, "db_pool_active": 4, "db_pool_max": 20},
    {"timestamp": "2026-09-27T08:00:00Z", "service": "pagos", "latency_p95_ms": 140, "request_count": 250, "error_count": 0, "db_pool_active": 4, "db_pool_max": 20},

    # 08:03 - Primeras demoras
    {"timestamp": "2026-09-27T08:03:00Z", "service": "pedidos", "latency_p95_ms": 650, "request_count": 350, "error_count": 10, "db_pool_active": 8, "db_pool_max": 20},
    {"timestamp": "2026-09-27T08:03:00Z", "service": "pagos", "latency_p95_ms": 450, "request_count": 350, "error_count": 5, "db_pool_active": 7, "db_pool_max": 20},

    # 08:06 - Demoras evidentes (pool pedidos 75% -> 15/20)
    {"timestamp": "2026-09-27T08:06:00Z", "service": "pedidos", "latency_p95_ms": 1500, "request_count": 500, "error_count": 120, "db_pool_active": 15, "db_pool_max": 20},
    {"timestamp": "2026-09-27T08:06:00Z", "service": "pagos", "latency_p95_ms": 1100, "request_count": 500, "error_count": 80, "db_pool_active": 13, "db_pool_max": 20},

    # 08:09 - Latencia en aumento, pool pagos a 18/20
    {"timestamp": "2026-09-27T08:09:00Z", "service": "pedidos", "latency_p95_ms": 3200, "request_count": 550, "error_count": 200, "db_pool_active": 18, "db_pool_max": 20},
    {"timestamp": "2026-09-27T08:09:00Z", "service": "pagos", "latency_p95_ms": 2800, "request_count": 550, "error_count": 180, "db_pool_active": 18, "db_pool_max": 20},

    # 08:12 - Capacidad máxima alcanzada (61% pedidos, 57% pagos)
    {"timestamp": "2026-09-27T08:12:00Z", "service": "pedidos", "latency_p95_ms": 7500, "request_count": 600, "error_count": 366, "db_pool_active": 20, "db_pool_max": 20},
    {"timestamp": "2026-09-27T08:12:00Z", "service": "pagos", "latency_p95_ms": 6700, "request_count": 600, "error_count": 342, "db_pool_active": 20, "db_pool_max": 20},

    # 08:14 - Pico crítico máximo: 64% pedidos (384/600), 59% pagos (354/600)
    {"timestamp": "2026-09-27T08:14:00Z", "service": "pedidos", "latency_p95_ms": 9800, "request_count": 600, "error_count": 384, "db_pool_active": 20, "db_pool_max": 20},
    {"timestamp": "2026-09-27T08:14:00Z", "service": "pagos", "latency_p95_ms": 8300, "request_count": 600, "error_count": 354, "db_pool_active": 20, "db_pool_max": 20},
    {"timestamp": "2026-09-27T08:14:00Z", "service": "catalogo", "latency_p95_ms": 60, "request_count": 320, "error_count": 0, "db_pool_active": 3, "db_pool_max": 20},
    {"timestamp": "2026-09-27T08:14:00Z", "service": "calificaciones", "latency_p95_ms": 60, "request_count": 160, "error_count": 0, "db_pool_active": 4, "db_pool_max": 20},

    # 08:16 - Reporte finaliza tras 16 minutos; inicia recuperación
    {"timestamp": "2026-09-27T08:16:00Z", "service": "pedidos", "latency_p95_ms": 4100, "request_count": 500, "error_count": 150, "db_pool_active": 16, "db_pool_max": 20},
    {"timestamp": "2026-09-27T08:16:00Z", "service": "pagos", "latency_p95_ms": 3200, "request_count": 500, "error_count": 120, "db_pool_active": 14, "db_pool_max": 20},

    # 08:19 - Pool pedidos normalizado (7/20)
    {"timestamp": "2026-09-27T08:19:00Z", "service": "pedidos", "latency_p95_ms": 800, "request_count": 350, "error_count": 10, "db_pool_active": 7, "db_pool_max": 20},
    {"timestamp": "2026-09-27T08:19:00Z", "service": "pagos", "latency_p95_ms": 700, "request_count": 350, "error_count": 8, "db_pool_active": 6, "db_pool_max": 20},

    # 08:22 - Normalización completa a línea base
    {"timestamp": "2026-09-27T08:22:00Z", "service": "pedidos", "latency_p95_ms": 120, "request_count": 200, "error_count": 0, "db_pool_active": 3, "db_pool_max": 20},
    {"timestamp": "2026-09-27T08:22:00Z", "service": "pagos", "latency_p95_ms": 120, "request_count": 200, "error_count": 0, "db_pool_active": 3, "db_pool_max": 20},
    {"timestamp": "2026-09-27T08:22:00Z", "service": "catalogo", "latency_p95_ms": 50, "request_count": 300, "error_count": 0, "db_pool_active": 3, "db_pool_max": 20},
    {"timestamp": "2026-09-27T08:22:00Z", "service": "calificaciones", "latency_p95_ms": 60, "request_count": 150, "error_count": 0, "db_pool_active": 4, "db_pool_max": 20},
]


def post_telemetry(item):
    data = json.dumps({
        **item,
        "environment": "production",
        "source": "arepa-go-api",
    }).encode("utf-8")
    req = Request(f"{BASE_URL}/api/telemetry", data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": BASE_URL,
    })
    try:
        with urlopen(req, timeout=5) as res:
            return res.status, json.loads(res.read().decode())
    except HTTPError as err:
        return err.code, json.loads(err.read().decode())
    except (URLError, TimeoutError) as err:
        raise RuntimeError(f"No se pudo conectar a {BASE_URL}: {err}") from err


def main():
    print(f"Iniciando carga de telemetría histórica del incidente (27-09-2026)...")
    success_count = 0
    for item in INCIDENT_TIMELINE:
        status, resp = post_telemetry(item)
        if status in (200, 201):
            success_count += 1
            action = resp.get("incident_evaluation", {}).get("action", "recorded")
            print(f"[{item['timestamp']}] {item['service']:<14} p95={item['latency_p95_ms']:>4}ms err={item['error_count']:>3}/{item['request_count']} -> {action}")
        else:
            print(f"Error {status} enviando {item['service']} ({item['timestamp']}): {resp}")
    print(f"\nCarga completada: {success_count}/{len(INCIDENT_TIMELINE)} mediciones registradas.")
    print("El dashboard del SOC reflejará ahora la curva de latencia y el historial del incidente.")


if __name__ == "__main__":
    main()
