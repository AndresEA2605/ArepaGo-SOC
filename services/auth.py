"""Internal authentication service for ArepaGo local and application APIs."""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from services.common import JsonHandler
from services.db import connection, init_pool, pool_stats
from services.migrations import apply_migrations
from services.rate_limit import SlidingWindowLimiter, resolve_client_ip

SCHEMA = "soc_auth"
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "12"))
if not 1 <= SESSION_HOURS <= 168:
    raise RuntimeError("SESSION_HOURS debe estar entre 1 y 168")
LOGIN_LIMIT = 5
LOGIN_IP_LIMIT = 30
REGISTER_LIMIT = 5
LOGIN_WINDOW_SECONDS = 60
_login_limiter = SlidingWindowLimiter()
_login_ip_limiter = SlidingWindowLimiter()
_registration_limiter = SlidingWindowLimiter()


def hash_password(password):
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=64)
    return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"


def verify_password(password, encoded):
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "scrypt":
            return False
        derived = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                 n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(digest_hex)))
        return hmac.compare_digest(derived.hex(), digest_hex)
    except (ValueError, TypeError, MemoryError):
        return False


_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def normalize_email(value):
    email = str(value or "").strip().lower()
    if len(email) > 254 or email.count("@") != 1 or any(char.isspace() for char in email):
        raise ValueError("Correo electrónico inválido")
    local, domain = email.split("@")
    if not local or not domain or "." not in domain:
        raise ValueError("Correo electrónico inválido")
    return email


def public_user(row):
    return {"id": str(row["id"]), "email": row["email"], "role": row["role"]}


def bearer_token(handler):
    value = handler.headers.get("Authorization", "")
    if value.startswith("Bearer "):
        return value[7:].strip()
    return ""


def token_digest(token):
    return hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()


def bootstrap_admin(db):
    email_value = os.environ.get("ADMIN_EMAIL")
    password_file = os.environ.get("ADMIN_PASSWORD_FILE")
    if not email_value and not password_file:
        return
    if not email_value or not password_file:
        raise RuntimeError("ADMIN_EMAIL y ADMIN_PASSWORD_FILE deben configurarse juntos")
    email = normalize_email(email_value)
    password = Path(password_file).read_text(encoding="utf-8").strip()
    if len(password) < 12:
        raise RuntimeError("La contraseña inicial de administración debe tener al menos 12 caracteres")
    inserted = db.execute(f"""INSERT INTO {SCHEMA}.users(email,password_hash,role)
        VALUES (%s,%s,'admin') ON CONFLICT (email) DO NOTHING RETURNING id""",
                          (email, hash_password(password))).fetchone()
    if inserted is None:
        existing = db.execute(f"SELECT role,disabled_at FROM {SCHEMA}.users WHERE email=%s", (email,)).fetchone()
        if existing is None or existing["role"] != "admin" or existing["disabled_at"] is not None:
            raise RuntimeError("ADMIN_EMAIL ya pertenece a una cuenta que no es un administrador activo")


MIGRATIONS = [
    (1, "users and sessions", [
        f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin','customer')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            disabled_at TIMESTAMPTZ
        )""",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.sessions (
            token_hash CHAR(64) PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES {SCHEMA}.users(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL
        )""",
        f"CREATE INDEX IF NOT EXISTS sessions_expiry ON {SCHEMA}.sessions(expires_at)",
    ]),
]


def initialize():
    init_pool("arepago-auth")
    with connection() as db:
        apply_migrations(db, "auth", MIGRATIONS)
        bootstrap_admin(db)


def request_ip(handler):
    """Resolve the real client address forwarded by the trusted gateway."""
    peer = handler.client_address[0] if getattr(handler, "client_address", None) else None
    return resolve_client_ip(handler.headers.get("X-Real-IP"),
                             handler.headers.get("X-Forwarded-For"), peer)


class Handler(JsonHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path in {"/health", "/health/detailed"}:
            try:
                with connection() as db:
                    db.execute("SELECT 1")
                details = {"service": "auth", "status": "ok", "database": "connected"}
                if path == "/health/detailed": details["database_pool"] = pool_stats()
                self.send_json(200, details)
            except Exception as exc:
                self.log_exception(exc, "health_check_failed")
                self.send_json(503, {"service": "auth", "status": "degraded", "database": "unavailable",
                                     "diagnostic": type(exc).__name__, "database_pool": pool_stats()})
            return
        if path == "/session":
            self.session()
            return
        self.send_json(404, {"error": "ruta no encontrada"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/register":
            self.register()
        elif path == "/login":
            self.login()
        elif path == "/logout":
            self.logout()
        else:
            self.send_json(404, {"error": "ruta no encontrada"})

    def login(self):
        try:
            payload = self.read_json()
            email = normalize_email(payload.get("email"))
            password = payload.get("password")
            if not isinstance(password, str) or not password or len(password) > 1024:
                raise ValueError("Credenciales inválidas")
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        ip = request_ip(self)
        account_key = f"{ip}|{email}"
        account_allowed = _login_limiter.allow(account_key, LOGIN_LIMIT, LOGIN_WINDOW_SECONDS)
        ip_allowed = _login_ip_limiter.allow(ip, LOGIN_IP_LIMIT, LOGIN_WINDOW_SECONDS)
        if not (account_allowed and ip_allowed):
            self.send_json(429, {"error": "Demasiados intentos. Intenta de nuevo en un minuto."})
            return
        with connection() as db:
            row = db.execute(f"SELECT * FROM {SCHEMA}.users WHERE email=%s AND disabled_at IS NULL", (email,)).fetchone()
        password_hash = row["password_hash"] if row is not None else _DUMMY_PASSWORD_HASH
        if not verify_password(password, password_hash) or row is None:
            self.send_json(401, {"error": "Correo o contraseña incorrectos"})
            return
        _login_limiter.reset(account_key)
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)
        with connection() as db:
            db.execute(f"DELETE FROM {SCHEMA}.sessions WHERE expires_at < now()")
            db.execute(f"INSERT INTO {SCHEMA}.sessions(token_hash,user_id,expires_at) VALUES (%s,%s,%s)",
                       (token_digest(token), row["id"], expires))
        self.send_json(200, {"user": public_user(row), "session_token": token})

    def register(self):
        try:
            payload = self.read_json()
            email = normalize_email(payload.get("email"))
            password = payload.get("password")
            if not isinstance(password, str) or len(password) < 12 or len(password) > 1024:
                raise ValueError("La contraseña debe tener entre 12 y 1024 caracteres")
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        ip = request_ip(self)
        if not _registration_limiter.allow(ip, REGISTER_LIMIT, LOGIN_WINDOW_SECONDS):
            self.send_json(429, {"error": "Demasiados intentos de registro. Intenta de nuevo en un minuto."})
            return
        with connection() as db:
            row = db.execute(f"""INSERT INTO {SCHEMA}.users(email,password_hash,role)
                VALUES (%s,%s,'customer') ON CONFLICT (email) DO NOTHING
                RETURNING id,email,role""", (email, hash_password(password))).fetchone()
        if row is None:
            self.send_json(409, {"error": "Ya existe una cuenta con ese correo"})
            return
        self.send_json(201, {"user": public_user(row)})

    def session(self):
        token = bearer_token(self)
        if not token:
            self.send_json(401, {"error": "autenticación requerida"})
            return
        with connection() as db:
            row = db.execute(f"""SELECT u.id,u.email,u.role FROM {SCHEMA}.sessions s
                JOIN {SCHEMA}.users u ON u.id=s.user_id
                WHERE s.token_hash=%s AND s.expires_at>now() AND u.disabled_at IS NULL""",
                (token_digest(token),)).fetchone()
        if row is None:
            self.send_json(401, {"error": "sesión inválida o expirada"})
            return
        self.send_json(200, {"user": public_user(row)})

    def logout(self):
        token = bearer_token(self)
        if token:
            with connection() as db:
                db.execute(f"DELETE FROM {SCHEMA}.sessions WHERE token_hash=%s", (token_digest(token),))
        self.send_json(200, {"ok": True})


def main():
    initialize()
    port = int(os.environ.get("PORT", "8004"))
    print(f"auth service listening on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
