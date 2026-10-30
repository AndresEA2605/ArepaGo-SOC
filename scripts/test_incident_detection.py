#!/usr/bin/env python3
"""Brief local Pedidos outage to verify gateway telemetry and incident detection."""
import argparse
import concurrent.futures
import http.cookiejar
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://localhost:8081"


def local_setting(name, default):
    value = os.environ.get(name)
    if value is not None:
        return value
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, configured = line.partition("=")
            if separator and key.strip() == name:
                return configured.strip().strip("\"'")
    return default


class LocalApi:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path, method="GET", payload=None, timeout=4):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base_url + path, data=data, method=method, headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "Origin": self.base_url,
        })
        try:
            with self.opener.open(request, timeout=timeout) as response:
                body = response.read().decode(errors="replace")
                return response.status, json.loads(body) if body else {}
        except HTTPError as error:
            body = error.read().decode(errors="replace")
            try:
                body = json.loads(body)
            except ValueError:
                pass
            return error.code, body


def docker_compose(args, use_sudo):
    prefix = ["sudo", "-n"] if use_sudo else []
    command = prefix + ["docker", "compose", "-f", "docker-compose.yml", "--profile", "local", *args]
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True)


def admin_login(api):
    password = os.environ.get("AREPAGO_ADMIN_PASSWORD")
    password_file = ROOT / "secrets" / "admin_password.txt"
    if not password and password_file.is_file():
        password = password_file.read_text(encoding="utf-8").strip()
    if not password:
        raise RuntimeError("No encuentro secrets/admin_password.txt. Configura AREPAGO_ADMIN_PASSWORD.")
    email = os.environ.get("AREPAGO_ADMIN_EMAIL", "admin@arepago.local")
    status, result = api.request("/api/auth/login", "POST", {"email": email, "password": password})
    if status != 200 or result.get("user", {}).get("role") != "admin":
        raise RuntimeError(f"No pude iniciar sesión como administrador (HTTP {status}): {result}")


def measurement_has_test_error(measurement, after):
    timestamp = measurement.get("timestamp", "")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    diagnostics = measurement.get("diagnostics") or []
    return parsed >= after and measurement.get("error_count", 0) > 0 and any(
        int(count) > 0 for item in diagnostics
        for code, count in (item.get("statuses") or {}).items() if str(code) in {"500", "502", "503", "504"}
    )


def incident_started_after(incident, after):
    try:
        started = datetime.fromisoformat(incident.get("started_at", "").replace("Z", "+00:00"))
    except ValueError:
        return False
    reasons = incident.get("reasons") or []
    return started >= after and any(item.get("signal") == "error_rate" for item in reasons)


def verify_current_state(api):
    health_status, health = api.request("/api/pedidos/health")
    telemetry_status, telemetry = api.request("/api/telemetry?service=pedidos&limit=50")
    open_status, open_data = api.request("/api/incidents?status=open")
    all_status, all_data = api.request("/api/incidents?status=all")
    if any(status != 200 for status in (health_status, telemetry_status, open_status, all_status)):
        print(f"ERROR: no se pudieron consultar todas las comprobaciones: health={health_status}, telemetry={telemetry_status}, open={open_status}, all={all_status}", file=sys.stderr)
        return 1
    measurements = telemetry.get("measurements", [])
    latest = measurements[0] if measurements else None
    old = datetime.min.replace(tzinfo=timezone.utc)
    detected = next((item for item in measurements if measurement_has_test_error(item, old)), None)
    open_orders = [item for item in open_data.get("incidents", []) if item.get("service") == "pedidos"]
    latest_incident = next((item for item in all_data.get("incidents", []) if item.get("service") == "pedidos"), None)
    print(f"Health de Pedidos: HTTP {health_status} · {health.get('status', health)}")
    if detected:
        print(f"Evidencia 5xx en Telemetry: {detected['error_count']}/{detected['request_count']} errores · {detected['timestamp']} · {detected.get('source')}")
    else:
        print("No se encontró un 5xx en las últimas 50 muestras de Pedidos.")
    if latest:
        print(f"Última muestra: {latest['error_count']}/{latest['request_count']} errores · {latest['timestamp']}")
    print(f"Incidentes abiertos de Pedidos: {len(open_orders)}")
    if latest_incident:
        print(f"Incidente más reciente: {latest_incident['id']} · {latest_incident['status']} · {latest_incident['severity']}")
    healthy_sample = latest and latest.get("request_count", 0) > 0 and latest.get("error_count", 0) == 0
    ok = health_status == 200 and bool(detected) and not open_orders and bool(healthy_sample)
    if ok:
        print("VERIFICACIÓN COMPLETA: el 5xx quedó medido, Pedidos está sano y el último incidente está cerrado.")
        return 0
    print("VERIFICACIÓN INCOMPLETA: revisa el estado indicado arriba; no se modificó ningún servicio ni dato.", file=sys.stderr)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=BASE_URL, help="URL local de ArepaGo (por defecto %(default)s)")
    parser.add_argument("--sudo-docker", action="store_true", help="Ejecutar Docker Compose con sudo -n; ejecuta sudo -v antes")
    parser.add_argument("--verify-only", action="store_true", help="Verificar health, Telemetry e incidentes sin inducir fallas ni usar Docker")
    args = parser.parse_args()
    api = LocalApi(args.url)

    try:
        admin_login(api)
        if args.verify_only:
            return verify_current_state(api)
        status, health = api.request("/api/pedidos/health")
        if status != 200:
            raise RuntimeError(f"Pedidos no está sano antes de la prueba (HTTP {status}): {health}")
        status, incident_data = api.request("/api/incidents?status=open")
        if status != 200:
            raise RuntimeError(f"No pude revisar incidentes abiertos (HTTP {status}): {incident_data}")
        existing = [item for item in incident_data.get("incidents", []) if item.get("service") == "pedidos"]
        if existing:
            raise RuntimeError("Ya existe un incidente abierto de Pedidos. Resuélvelo antes de generar esta prueba para no alterar un incidente en curso.")
        running = docker_compose(["ps", "-q", "pedidos-service"], args.sudo_docker).stdout.strip().splitlines()
        if not running:
            raise RuntimeError("No encuentro contenedores activos de pedidos-service en el perfil local de docker-compose.yml.")
    except (RuntimeError, subprocess.CalledProcessError, URLError, TimeoutError) as error:
        details = getattr(error, "stderr", None) or str(error)
        print(f"ERROR: {error}\n{details}", file=sys.stderr)
        if "permission denied" in details.lower():
            print("Prueba `sudo -v` y ejecuta de nuevo con --sudo-docker.", file=sys.stderr)
        return 2

    fault_started = datetime.now(timezone.utc)
    responses = []
    stopped = False
    workers = concurrent.futures.ThreadPoolExecutor(max_workers=12)
    futures = []
    try:
        docker_compose(["stop", "--timeout", "1", "pedidos-service"], args.sudo_docker)
        stopped = True
        fault_started = datetime.now(timezone.utc)
        futures = [workers.submit(api.request, "/api/pedidos/health", timeout=3) for _ in range(12)]
        time.sleep(1)
    except (subprocess.CalledProcessError, OSError) as error:
        print(f"ERROR al iniciar la falla controlada: {error}", file=sys.stderr)
        return 2
    finally:
        if stopped:
            try:
                docker_compose(["start", "pedidos-service"], args.sudo_docker)
                print("Se solicitó iniciar Pedidos tras 1 segundo de interrupción controlada.")
            except (subprocess.CalledProcessError, OSError) as error:
                print(f"CRÍTICO: no se pudo iniciar Pedidos automáticamente: {error}", file=sys.stderr)
                print("Recupéralo con: sudo docker compose -f docker-compose.yml --profile local start pedidos-service", file=sys.stderr)
                workers.shutdown(wait=False, cancel_futures=True)
                return 3
    health_deadline = time.monotonic() + 25
    while time.monotonic() < health_deadline:
        try:
            health_status, _ = api.request("/api/pedidos/health", timeout=2)
            if health_status == 200:
                break
        except (URLError, TimeoutError):
            pass
        time.sleep(1)
    else:
        print("CRÍTICO: Pedidos no respondió sano después del reinicio.", file=sys.stderr)
        return 3
    for future in futures:
        try:
            responses.append(future.result(timeout=4)[0])
        except Exception:
            responses.append(None)
    workers.shutdown(wait=True, cancel_futures=True)
    failures = sum(code is not None and code >= 500 for code in responses)
    print(f"Solicitudes de prueba: {len(responses)} · respuestas HTTP 5xx observadas: {failures} · estados: {responses}")
    if failures == 0:
        print("No se observó un 5xx durante la ventana; no afirmaré que la detección funcionó.", file=sys.stderr)
        return 1

    deadline = time.monotonic() + 55
    measurement = None
    incident = None
    while time.monotonic() < deadline:
        status, data = api.request("/api/telemetry?service=pedidos&limit=50")
        if status == 200:
            measurement = next((item for item in data.get("measurements", [])
                                if measurement_has_test_error(item, fault_started)), None)
        if measurement:
            status, data = api.request("/api/incidents?status=all")
            if status == 200:
                incident = next((item for item in data.get("incidents", [])
                                 if item.get("service") == "pedidos" and incident_started_after(item, fault_started)), None)
            if incident:
                break
        time.sleep(2)

    if not measurement:
        print("ERROR: llegaron 5xx, pero Telemetry no guardó una medición con ese error en 55 segundos.", file=sys.stderr)
        return 1
    if not incident:
        print(f"Telemetry registró {measurement.get('error_count')}/{measurement.get('request_count')} errores, pero no apareció incidente abierto. Revisa el umbral y los logs.", file=sys.stderr)
        return 1

    print(f"DETECCIÓN OK: Telemetry guardó {measurement['error_count']}/{measurement['request_count']} errores de Pedidos; incidente {incident['id']} ({incident['severity']}) quedó {incident['status']}.")
    if incident.get("status") == "resolved":
        print("RECUPERACIÓN OK: el flujo normal ya resolvió el incidente con una muestra sana.")
        return 0
    recovery_status, recovery = api.request("/api/pedidos/health")
    if recovery_status != 200:
        print(f"ERROR: Pedidos aún no se recupera (HTTP {recovery_status}): {recovery}", file=sys.stderr)
        return 1
    recovery_samples = max(2, min(10, int(local_setting("INCIDENT_RECOVERY_SAMPLES", "3"))))
    sample_interval = max(1.0, float(local_setting("METRICS_SAMPLE_SECONDS", "15")))
    recovery_timeout = max(40.0, sample_interval * (recovery_samples + 1) + 5.0)
    deadline = time.monotonic() + recovery_timeout
    resolved = False
    while time.monotonic() < deadline:
        status, current = api.request(f"/api/incidents/{incident['id']}")
        if status == 200 and current.get("status") == "resolved":
            resolved = True
            break
        time.sleep(2)
    if resolved:
        print(f"RECUPERACIÓN OK: {recovery_samples} mediciones sanas consecutivas validaron la recuperación.")
    else:
        print("La falla fue detectada, pero el incidente sigue abierto; revisa las siguientes muestras de Pedidos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
