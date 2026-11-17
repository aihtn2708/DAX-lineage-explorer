"""One way to talk to the workspace database, whether it is a SQLite file or Postgres.

    Store("pbi-workspace.db")                                      # SQLite: a pilot on one PC
    Store("postgresql://workspace@db.corp/powerbi")                # Postgres: the team server
    Store("postgresql://workspace@db.corp/powerbi?schema=finance") # its own schema in a shared database

The rest of the workspace writes SQLite-style SQL (``?`` placeholders,
``fold()`` for case-insensitive text, ``INSERT OR IGNORE``, ``lastrowid``) and
this module turns it into Postgres when that is the database. Rows come back
the same from both: readable by column name and by position, numbers as ints.

Postgres needs the psycopg package: ``pip install "psycopg[binary]"``.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ID_TABLES = {"folder", "model", "model_version", "object", "comment", "scan_run", "change_request", "change_event",
             "account", "audit"}


def is_postgres(location) -> bool:
    return str(location).startswith(("postgresql://", "postgres://"))


def mask(location) -> str:
    """The location with any password hidden, for messages."""
    text = str(location)
    if not is_postgres(text):
        return text
    parts = urlsplit(text)
    if parts.password:
        netloc = parts.netloc.replace(f":{parts.password}@", ":•••@")
        return urlunsplit(parts._replace(netloc=netloc))
    return text


def _fold(text) -> str:
    return (text or "").casefold()


class SQLite:
    kind = "sqlite"

    def __init__(self, location):
        self.path = Path(location)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.create_function("fold", 1, _fold, deterministic=True)
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def tables(self) -> list:
        with self.connect() as con:
            return [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]


# -- Postgres ---------------------------------------------------------------------------

class Row(dict):
    """A row readable by column name and by position, like sqlite3.Row."""

    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return dict.__getitem__(self, key)


def _plain(value):
    if isinstance(value, Decimal):  # SUM() of integers is numeric in Postgres
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _row_factory(cursor):
    names = [d.name for d in cursor.description] if cursor.description else []
    return lambda values: Row(names, [_plain(v) for v in values])


_INSERT_IGNORE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO", re.I)
_INSERT_TABLE = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.I)


def translate(sql: str) -> tuple:
    """SQLite-style SQL as Postgres; returns (sql, the table an INSERT adds an id to, or None)."""
    ignore = bool(_INSERT_IGNORE.match(sql))
    if ignore:
        sql = _INSERT_IGNORE.sub("INSERT INTO", sql, count=1)
    sql = sql.replace("%", "%%").replace("?", "%s")
    sql = re.sub(r"\bfold\(", "lower(", sql)
    if ignore:
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    match = _INSERT_TABLE.match(sql)
    table = match.group(1).lower() if match and match.group(1).lower() in ID_TABLES else None
    return sql, table


def translate_ddl(script: str) -> list:
    script = re.sub(r"\bINTEGER\b", "BIGINT", script)
    script = re.sub(r"(?<!\w)id\s+BIGINT\s+PRIMARY KEY", "id BIGSERIAL PRIMARY KEY", script)
    return [s.strip() for s in script.split(";") if s.strip()]


class _Cursor:
    def __init__(self, cursor, lastrowid=None):
        self._cursor, self.lastrowid = cursor, lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone() if self._cursor.description else None

    def fetchall(self):
        return self._cursor.fetchall() if self._cursor.description else []

    def __iter__(self):
        return iter(self.fetchall())


class _Connection:
    def __init__(self, con):
        self._con = con

    def execute(self, sql: str, params=()):
        text, table = translate(sql)
        returning = table is not None and " returning " not in text.lower()
        if returning:
            text += " RETURNING id"
        cursor = self._con.cursor()
        cursor.execute(text, tuple(params))
        lastrowid = None
        if returning and cursor.description:
            row = cursor.fetchone()
            lastrowid = row[0] if row else None
        return _Cursor(cursor, lastrowid)

    def executemany(self, sql: str, rows):
        rows = list(rows)
        if not rows:
            return _Cursor(self._con.cursor())
        text, _ = translate(sql)
        cursor = self._con.cursor()
        cursor.executemany(text, [tuple(r) for r in rows])
        return _Cursor(cursor)

    def executescript(self, script: str):
        for statement in translate_ddl(script):
            self._con.execute(statement)


class Postgres:
    kind = "postgres"

    def __init__(self, location: str):
        try:
            import psycopg
        except ImportError:
            raise RuntimeError('a Postgres workspace needs the psycopg package: pip install "psycopg[binary]"') from None
        self._psycopg = psycopg
        parts = urlsplit(location)
        query = dict(parse_qsl(parts.query))
        self.schema = query.pop("schema", "")
        if self.schema and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", self.schema):
            raise ValueError("schema must be a plain name: letters, digits and _")
        self.conninfo = urlunsplit(parts._replace(query=urlencode(query)))
        self.path = location
        if self.schema:
            with self._raw() as con:
                con.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
                con.commit()

    def _raw(self):
        options = {"options": f"-c search_path={self.schema}"} if self.schema else {}
        return self._psycopg.connect(self.conninfo, cursor_factory=self._psycopg.ClientCursor,
                                     row_factory=_row_factory, connect_timeout=15, **options)

    @contextmanager
    def connect(self):
        con = self._raw()
        try:
            yield _Connection(con)
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def tables(self) -> list:
        with self.connect() as con:
            return [r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
                "ORDER BY table_name")]


def open_database(location):
    return Postgres(str(location)) if is_postgres(location) else SQLite(location)
