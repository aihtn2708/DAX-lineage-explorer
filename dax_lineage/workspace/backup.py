"""Back up a workspace to one file, and restore it into an empty workspace of either kind.

    daxlin workspace --db pbi-workspace.db backup --out workspace-2026-11-18.jsonl.gz
    daxlin workspace --db postgresql://workspace@db.corp/powerbi restore workspace-2026-11-18.jsonl.gz

The file is gzip-compressed JSON lines: a header, then each table's column names and rows.
It does not depend on the database it came from, so restoring a SQLite pilot into
Postgres is how a team moves to its server. Sign-in sessions are not kept.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

from .store import SCHEMA_VERSION, Store

# parents before children, so foreign keys hold while restoring
TABLES = ["meta", "folder", "model", "metric", "metric_variant", "model_version", "object", "dependency", "visual", "usage", "annotation",
          "review", "comment", "rule_setting", "rule_exception", "version_check", "finding", "change_request",
          "change_op", "change_event", "account", "api_token", "audit", "scan_run"]
FORMAT = "daxlin-workspace-backup"
CHUNK = 5000


def _columns(con, table: str) -> list:
    row = con.execute(f"SELECT * FROM {table} LIMIT 1").fetchone()
    if row is not None:
        return list(row.keys())
    return []


def backup(store: Store, out) -> dict:
    out = Path(out)
    counts = {}
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"format": FORMAT, "schema": SCHEMA_VERSION, "database": store.kind,
                             "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n")
        with store.connect() as con:
            for table in TABLES:
                columns = _columns(con, table)
                fh.write(json.dumps({"table": table, "columns": columns}) + "\n")
                order = "id" if "id" in columns else ", ".join(columns[:3]) or "1"
                offset, total = 0, 0
                while columns:
                    rows = con.execute(f"SELECT * FROM {table} ORDER BY {order} LIMIT ? OFFSET ?",
                                       (CHUNK, offset)).fetchall()
                    for r in rows:
                        fh.write(json.dumps([r[c] for c in columns], ensure_ascii=False) + "\n")
                    total += len(rows)
                    if len(rows) < CHUNK:
                        break
                    offset += CHUNK
                counts[table] = total
    return counts


def restore(store: Store, source, force: bool = False) -> dict:
    with store.connect() as con:
        busy = con.execute("SELECT COUNT(*) FROM model").fetchone()[0] + con.execute(
            "SELECT COUNT(*) FROM account").fetchone()[0]
    if busy and not force:
        raise ValueError("this workspace is not empty: restore into a new one, or pass --force to replace it")
    counts = {}
    with gzip.open(source, "rt", encoding="utf-8") as fh:
        header = json.loads(fh.readline() or "{}")
        if header.get("format") != FORMAT:
            raise ValueError(f"{source} is not a workspace backup")
        if int(header.get("schema", 0)) > SCHEMA_VERSION:
            raise ValueError("this backup was made by a newer daxlin: update daxlin first")
        with store.connect() as con:
            for table in reversed(TABLES + ["login_session"]):
                con.execute(f"DELETE FROM {table}")
            table, columns, batch = None, [], []

            def flush():
                if batch:
                    marks = ", ".join("?" * len(columns))
                    con.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})", batch)
                    counts[table] = counts.get(table, 0) + len(batch)
                    batch.clear()

            for line in fh:
                item = json.loads(line)
                if isinstance(item, dict):
                    flush()
                    table, columns = item["table"], item["columns"]
                    if table not in TABLES:
                        raise ValueError(f"unknown table {table} in the backup")
                    if table == "meta":
                        con.execute("DELETE FROM meta")
                    continue
                batch.append(item)
                if len(batch) >= CHUNK:
                    flush()
            flush()
            con.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),))
            if store.kind == "postgres":  # carry on numbering after the restored ids
                for name in TABLES:
                    if name in ("folder", "model", "model_version", "object", "comment", "scan_run", "change_request",
                                "change_event", "account", "audit", "metric"):
                        con.execute(f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), "
                                    f"COALESCE((SELECT MAX(id) FROM {name}), 0) + 1, false)")
    return counts
