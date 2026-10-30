"""PostgreSQL connection pool shared by this service process."""
import os
import hashlib
import time
from contextlib import contextmanager
from pathlib import Path
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from services.common import log_event

_pool = None
SLOW_QUERY_MS = float(os.environ.get("SLOW_QUERY_MS", "500"))


class ObservedConnection:
    """Time SQL execution without recording SQL parameters or customer data."""
    def __init__(self, connection):
        self._connection = connection

    def execute(self, query, *args, **kwargs):
        started = time.perf_counter()
        try:
            return self._connection.execute(query, *args, **kwargs)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            if duration_ms >= SLOW_QUERY_MS:
                statement = str(query).strip()
                operation = statement.split(None, 1)[0].upper() if statement else "UNKNOWN"
                fingerprint = hashlib.sha256(" ".join(statement.split()).encode("utf-8")).hexdigest()[:16]
                log_event("slow_query", level="warning", service=os.environ.get("SERVICE_NAME", "database"),
                          operation=operation, fingerprint=fingerprint, duration_ms=round(duration_ms, 3),
                          threshold_ms=SLOW_QUERY_MS)

def pool_stats():
    if _pool is None:
        return {"status": "uninitialized", "active": None, "available": None, "waiting": None,
                "max": int(os.environ.get("DB_POOL_MAX_SIZE", "10")), "acquire_errors": None}
    stats = _pool.get_stats()
    size = int(stats.get("pool_size", 0))
    available = int(stats.get("pool_available", 0))
    return {"status": "ok", "active": max(0, size - available), "available": available,
            "waiting": int(stats.get("requests_waiting", 0)),
            "max": int(stats.get("pool_max", os.environ.get("DB_POOL_MAX_SIZE", "10"))),
            "acquire_errors": int(stats.get("requests_errors", 0)),
            "requests": int(stats.get("requests_num", 0)),
            "wait_ms_total": round(float(stats.get("requests_wait_ms", 0)), 3)}

def init_pool(application_name):
    global _pool
    password_file = os.environ.get("DB_PASSWORD_FILE")
    password = Path(password_file).read_text(encoding="utf-8").strip() if password_file else os.environ.get("DB_PASSWORD")
    if not password:
        raise RuntimeError("Falta DB_PASSWORD_FILE o DB_PASSWORD para conectarse a PostgreSQL")
    sslmode = os.environ.get("DB_SSLMODE", "disable")
    rootcert = os.environ.get("DB_SSLROOTCERT")
    if sslmode in {"verify-ca", "verify-full"} and not rootcert:
        raise RuntimeError("DB_SSLROOTCERT es obligatorio cuando DB_SSLMODE verifica certificados")
    params = {
        "host": os.environ.get("DB_HOST", "postgres"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": os.environ.get("DB_NAME", "arepagosoc"),
        "user": os.environ.get("DB_USER", "arepagosoc_app"),
        "password": password,
        "sslmode": sslmode,
        "connect_timeout": int(os.environ.get("DB_CONNECT_TIMEOUT", "5")),
        "application_name": application_name,
        "options": " ".join([
            f"-c statement_timeout={int(os.environ.get('DB_STATEMENT_TIMEOUT_MS', '15000'))}",
            f"-c lock_timeout={int(os.environ.get('DB_LOCK_TIMEOUT_MS', '5000'))}",
            f"-c idle_in_transaction_session_timeout={int(os.environ.get('DB_IDLE_TX_TIMEOUT_MS', '30000'))}",
        ]),
    }
    if rootcert:
        params["sslrootcert"] = rootcert
    conninfo = make_conninfo(**params)
    min_size = int(os.environ.get("DB_POOL_MIN_SIZE", "1"))
    max_size = int(os.environ.get("DB_POOL_MAX_SIZE", "10"))
    if min_size < 0 or max_size < 1 or min_size > max_size:
        raise RuntimeError("DB_POOL_MIN_SIZE debe ser >= 0 y DB_POOL_MAX_SIZE debe ser >= DB_POOL_MIN_SIZE")
    _pool = ConnectionPool(
        conninfo=conninfo,
        min_size=min_size,
        max_size=max_size,
        timeout=float(os.environ.get("DB_POOL_TIMEOUT_SECONDS", "10")),
        max_lifetime=float(os.environ.get("DB_POOL_MAX_LIFETIME_SECONDS", "1800")),
        max_idle=float(os.environ.get("DB_POOL_MAX_IDLE_SECONDS", "300")),
        check=ConnectionPool.check_connection,
        kwargs={"row_factory": dict_row},
        name=application_name,
        open=False,
    )
    _pool.open(wait=True, timeout=float(os.environ.get("DB_STARTUP_TIMEOUT_SECONDS", "45")))
    return _pool

@contextmanager
def connection():
    if _pool is None:
        raise RuntimeError("El pool de PostgreSQL no ha sido inicializado")
    with _pool.connection() as conn:
        yield ObservedConnection(conn)

@contextmanager
def autocommit_connection():
    """Check out a connection for PostgreSQL commands that cannot run in a transaction."""
    if _pool is None:
        raise RuntimeError("El pool de PostgreSQL no ha sido inicializado")
    with _pool.connection() as conn:
        previous = conn.autocommit
        conn.autocommit = True
        try:
            yield ObservedConnection(conn)
        finally:
            conn.autocommit = previous

def close_pool():
    if _pool is not None:
        _pool.close()
