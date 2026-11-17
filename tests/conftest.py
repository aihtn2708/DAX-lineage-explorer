"""Run the workspace tests against Postgres as well as SQLite.

    DAXLIN_TEST_POSTGRES=postgresql://postgres@127.0.0.1:5432/ws_test python -m pytest tests

Every workspace a test opens as a file goes instead to its own schema in that
database, so the same tests check both backends.
"""
import hashlib
import os
from pathlib import Path

POSTGRES = os.environ.get("DAXLIN_TEST_POSTGRES")

if POSTGRES:
    from dax_lineage.workspace import db as db_module
    from dax_lineage.workspace import store as store_module

    _open = db_module.open_database

    def _schema_per_file(location):
        if db_module.is_postgres(location):
            return _open(location)
        schema = "t_" + hashlib.sha1(str(Path(location).resolve()).encode()).hexdigest()[:20]
        return _open(f"{POSTGRES}{'&' if '?' in POSTGRES else '?'}schema={schema}")

    store_module.open_database = _schema_per_file

    def pytest_sessionstart(session):
        import psycopg
        with psycopg.connect(POSTGRES, autocommit=True) as con:
            for (name,) in con.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 't\\_%'").fetchall():
                con.execute(f'DROP SCHEMA "{name}" CASCADE')

    def pytest_report_header(config):
        return f"workspace database: Postgres ({db_module.mask(POSTGRES)})"
