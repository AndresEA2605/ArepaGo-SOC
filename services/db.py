"""PostgreSQL connection pool shared by this service process."""
import os
from pathlib import Path
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool = None

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
    }
    if rootcert:
        params["sslrootcert"] = rootcert
    conninfo = make_conninfo(**params)
    _pool = ConnectionPool(
        conninfo=conninfo,
        min_size=int(os.environ.get("DB_POOL_MIN_SIZE", "1")),
        max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "5")),
        timeout=float(os.environ.get("DB_POOL_TIMEOUT_SECONDS", "10")),
        kwargs={"row_factory": dict_row},
        name=application_name,
        open=False,
    )
    _pool.open(wait=True, timeout=float(os.environ.get("DB_STARTUP_TIMEOUT_SECONDS", "45")))
    return _pool

def connection():
    if _pool is None:
        raise RuntimeError("El pool de PostgreSQL no ha sido inicializado")
    return _pool.connection()

def close_pool():
    if _pool is not None:
        _pool.close()
