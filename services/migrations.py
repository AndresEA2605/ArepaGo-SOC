"""Versioned, transactional schema migrations shared by every service.

Each module owns a list of ``(version, name, statements)`` entries. The runner
creates ``soc_migrations.schema_migrations`` and applies pending versions inside
the caller's transaction. A transaction-scoped advisory lock serialises
concurrent startups and is released automatically on commit or rollback, so a
failed migration never leaves a lock behind in a pooled connection.
"""

MIGRATIONS_SCHEMA = "soc_migrations"
MIGRATIONS_LOCK = "arepago-migrations"


def _validate(migrations):
    versions = [migration[0] for migration in migrations]
    if versions != sorted(set(versions)):
        raise ValueError("las versiones de migración deben ser enteros únicos y ascendentes")
    for version, name, statements in migrations:
        if not isinstance(version, int) or version < 1:
            raise ValueError("cada migración debe tener una versión entera >= 1")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("cada migración debe tener un nombre")
        if not statements:
            raise ValueError(f"la migración {version} no tiene sentencias")


def apply_migrations(db, module, migrations):
    """Apply pending migrations for ``module`` inside the caller's transaction."""
    _validate(migrations)
    db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (MIGRATIONS_LOCK,))
    db.execute(f"CREATE SCHEMA IF NOT EXISTS {MIGRATIONS_SCHEMA}")
    db.execute(f"""CREATE TABLE IF NOT EXISTS {MIGRATIONS_SCHEMA}.schema_migrations (
        module TEXT NOT NULL,
        version INTEGER NOT NULL,
        name TEXT NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (module, version)
    )""")
    applied = {row["version"] for row in db.execute(
        f"SELECT version FROM {MIGRATIONS_SCHEMA}.schema_migrations WHERE module=%s",
        (module,)).fetchall()}
    for version, name, statements in migrations:
        if version in applied:
            continue
        for statement in statements:
            db.execute(statement)
        db.execute(f"""INSERT INTO {MIGRATIONS_SCHEMA}.schema_migrations (module, version, name)
            VALUES (%s, %s, %s)""", (module, version, name))
