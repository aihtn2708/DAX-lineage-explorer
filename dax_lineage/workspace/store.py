"""The workspace database: every scanned version of every model, and the team's notes on it.

One SQLite file, opened with a fresh connection per call so the web server's
threads and a scan can share it. The SQL is kept plain (no SQLite-only types)
so the same schema moves to Postgres for the team server in milestone 5.

Objects are matched across versions by ``object_key``: the object's LineageTag,
which Power BI keeps through renames, or its node id ("m:total sales") when a
model has no tags. Notes, owners and keep/remove decisions hang off that key,
so they follow a measure from one saved version to the next.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

SCHEMA_VERSION = 1
DECISIONS = ("", "keep", "remove")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS folder (
    id       INTEGER PRIMARY KEY,
    path     TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model (
    id                 INTEGER PRIMARY KEY,
    path               TEXT NOT NULL UNIQUE,
    name               TEXT NOT NULL,
    folder_id          INTEGER REFERENCES folder(id),
    kind               TEXT NOT NULL DEFAULT 'model',
    owner              TEXT NOT NULL DEFAULT '',
    team               TEXT NOT NULL DEFAULT '',
    notes              TEXT NOT NULL DEFAULT '',
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    missing            INTEGER NOT NULL DEFAULT 0,
    current_version_id INTEGER
);
CREATE TABLE IF NOT EXISTS model_version (
    id               INTEGER PRIMARY KEY,
    model_id         INTEGER NOT NULL REFERENCES model(id),
    number           INTEGER NOT NULL,
    file_hash        TEXT NOT NULL,
    file_size        INTEGER NOT NULL,
    file_modified_at TEXT NOT NULL,
    scanned_at       TEXT NOT NULL,
    status           TEXT NOT NULL,
    note             TEXT NOT NULL DEFAULT '',
    found            TEXT NOT NULL DEFAULT '',
    measures         INTEGER NOT NULL DEFAULT 0,
    columns          INTEGER NOT NULL DEFAULT 0,
    tables           INTEGER NOT NULL DEFAULT 0,
    visuals          INTEGER NOT NULL DEFAULT 0,
    pages            INTEGER NOT NULL DEFAULT 0,
    unused_measures  INTEGER NOT NULL DEFAULT 0,
    unused_columns   INTEGER NOT NULL DEFAULT 0,
    broken_refs      INTEGER NOT NULL DEFAULT 0,
    has_report       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (model_id, number)
);
CREATE TABLE IF NOT EXISTS object (
    id            INTEGER PRIMARY KEY,
    version_id    INTEGER NOT NULL REFERENCES model_version(id),
    node          TEXT NOT NULL,
    object_key    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    table_name    TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL,
    expression    TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    display_folder TEXT NOT NULL DEFAULT '',
    format_string TEXT NOT NULL DEFAULT '',
    data_type     TEXT NOT NULL DEFAULT '',
    is_hidden     INTEGER NOT NULL DEFAULT 0,
    is_calculated INTEGER NOT NULL DEFAULT 0,
    state         TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    depth         INTEGER NOT NULL DEFAULT 0,
    in_model      INTEGER NOT NULL DEFAULT 1,
    lineage_tag   TEXT NOT NULL DEFAULT '',
    usage_status  TEXT NOT NULL DEFAULT '',
    usage         TEXT NOT NULL DEFAULT '',
    reads         INTEGER NOT NULL DEFAULT 0,
    read_by       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (version_id, node)
);
CREATE INDEX IF NOT EXISTS object_key_idx ON object (object_key);
CREATE INDEX IF NOT EXISTS object_version_idx ON object (version_id, kind);
CREATE TABLE IF NOT EXISTS dependency (
    version_id   INTEGER NOT NULL REFERENCES model_version(id),
    source       TEXT NOT NULL,
    target       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    via_variable INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (version_id, source, target, kind)
);
CREATE INDEX IF NOT EXISTS dependency_target_idx ON dependency (version_id, target);
CREATE TABLE IF NOT EXISTS visual (
    version_id  INTEGER NOT NULL REFERENCES model_version(id),
    node        TEXT NOT NULL,
    report      TEXT NOT NULL DEFAULT '',
    page        TEXT NOT NULL DEFAULT '',
    page_order  INTEGER NOT NULL DEFAULT 0,
    visual_type TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (version_id, node)
);
CREATE TABLE IF NOT EXISTS usage (
    version_id INTEGER NOT NULL REFERENCES model_version(id),
    visual     TEXT NOT NULL,
    field      TEXT NOT NULL,
    roles      TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (version_id, visual, field)
);
CREATE INDEX IF NOT EXISTS usage_field_idx ON usage (version_id, field);
CREATE TABLE IF NOT EXISTS annotation (
    model_id   INTEGER NOT NULL REFERENCES model(id),
    object_key TEXT NOT NULL,
    owner      TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    decision   TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (model_id, object_key)
);
CREATE TABLE IF NOT EXISTS scan_run (
    id           INTEGER PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    folders      INTEGER NOT NULL DEFAULT 0,
    files        INTEGER NOT NULL DEFAULT 0,
    new_versions INTEGER NOT NULL DEFAULT 0,
    unchanged    INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    log          TEXT NOT NULL DEFAULT ''
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_db_path() -> Path:
    return Path(os.environ.get("DAXLIN_WORKSPACE_DB") or "pbi-workspace.db")


def _fold(text) -> str:
    return (text or "").casefold()


def _rows(cursor) -> list:
    return [dict(r) for r in cursor.fetchall()]


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)
            found = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if found is None:
                con.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(found["value"]) > SCHEMA_VERSION:
                raise RuntimeError(f"{self.path} was made by a newer daxlin (schema {found['value']}); update daxlin")

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

    # -- folders ----------------------------------------------------------

    def add_folder(self, path) -> dict:
        path = str(Path(path).expanduser().resolve())
        with self.connect() as con:
            con.execute("INSERT OR IGNORE INTO folder (path, added_at) VALUES (?, ?)", (path, now()))
            return dict(con.execute("SELECT * FROM folder WHERE path = ?", (path,)).fetchone())

    def remove_folder(self, path) -> bool:
        path = str(Path(path).expanduser().resolve())
        with self.connect() as con:
            return con.execute("DELETE FROM folder WHERE path = ?", (path,)).rowcount > 0

    def folders(self) -> list:
        with self.connect() as con:
            return _rows(con.execute("SELECT * FROM folder ORDER BY path"))

    # -- scanning ---------------------------------------------------------

    def start_run(self, folders: int) -> int:
        with self.connect() as con:
            return con.execute("INSERT INTO scan_run (started_at, folders) VALUES (?, ?)", (now(), folders)).lastrowid

    def finish_run(self, run_id: int, summary: dict) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE scan_run SET finished_at = ?, files = ?, new_versions = ?, unchanged = ?, failed = ?, log = ? "
                "WHERE id = ?",
                (now(), summary["files"], summary["new_versions"], summary["unchanged"], summary["failed"],
                 "\n".join(summary["log"])[-20000:], run_id))

    def last_run(self) -> Optional[dict]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM scan_run ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def model_by_path(self, path: str) -> Optional[dict]:
        with self.connect() as con:
            row = con.execute(
                "SELECT m.*, v.file_hash, v.file_size, v.file_modified_at FROM model m "
                "LEFT JOIN model_version v ON v.id = m.current_version_id WHERE m.path = ?", (path,)).fetchone()
            return dict(row) if row else None

    def touch(self, model_id: int) -> None:
        with self.connect() as con:
            con.execute("UPDATE model SET last_seen_at = ?, missing = 0 WHERE id = ?", (now(), model_id))

    def mark_missing(self, folder_path: str, seen: Iterable[str]) -> int:
        """Models under ``folder_path`` whose file was not found this scan."""
        seen = set(seen)
        prefix = folder_path.rstrip("/\\")
        with self.connect() as con:
            rows = con.execute("SELECT id, path FROM model WHERE missing = 0").fetchall()
            gone = [r["id"] for r in rows
                    if (r["path"] == prefix or r["path"].startswith(prefix + os.sep) or r["path"].startswith(prefix + "/"))
                    and r["path"] not in seen]
            con.executemany("UPDATE model SET missing = 1 WHERE id = ?", [(i,) for i in gone])
            return len(gone)

    def record_version(self, *, path: str, name: str, folder_id: Optional[int], kind: str, file_hash: str,
                       file_size: int, file_modified_at: str, status: str, note: str = "", found: str = "",
                       counts: Optional[dict] = None, objects: Iterable[dict] = (), dependencies: Iterable[dict] = (),
                       visuals: Iterable[dict] = (), usages: Iterable[dict] = ()) -> dict:
        """Store one scanned state of a file as its newest version, in one transaction."""
        stamp = now()
        counts = counts or {}
        with self.connect() as con:
            row = con.execute("SELECT id FROM model WHERE path = ?", (path,)).fetchone()
            if row is None:
                model_id = con.execute(
                    "INSERT INTO model (path, name, folder_id, kind, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
                    (path, name, folder_id, kind, stamp, stamp)).lastrowid
            else:
                model_id = row["id"]
                con.execute("UPDATE model SET name = ?, kind = ?, last_seen_at = ?, missing = 0, "
                            "folder_id = COALESCE(?, folder_id) WHERE id = ?",
                            (name, kind, stamp, folder_id, model_id))
            number = con.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM model_version WHERE model_id = ?",
                                 (model_id,)).fetchone()[0]
            cols = ("measures", "columns", "tables", "visuals", "pages", "unused_measures", "unused_columns",
                    "broken_refs", "has_report")
            version_id = con.execute(
                f"INSERT INTO model_version (model_id, number, file_hash, file_size, file_modified_at, scanned_at, "
                f"status, note, found, {', '.join(cols)}) VALUES ({', '.join('?' * (9 + len(cols)))})",
                (model_id, number, file_hash, file_size, file_modified_at, stamp, status, note, found,
                 *[int(counts.get(c, 0)) for c in cols])).lastrowid
            con.executemany(
                "INSERT INTO object (version_id, node, object_key, kind, table_name, name, expression, description, "
                "display_folder, format_string, data_type, is_hidden, is_calculated, state, error_message, depth, "
                "in_model, lineage_tag, usage_status, usage, reads, read_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(version_id, o["node"], o["object_key"], o["kind"], o.get("table_name", ""), o["name"],
                  o.get("expression", ""), o.get("description", ""), o.get("display_folder", ""),
                  o.get("format_string", ""), o.get("data_type", ""), int(bool(o.get("is_hidden"))),
                  int(bool(o.get("is_calculated"))), o.get("state", ""), o.get("error_message", ""),
                  int(o.get("depth", 0)), int(o.get("in_model", True)), o.get("lineage_tag", ""),
                  o.get("usage_status", ""), json.dumps(o["usage"]) if o.get("usage") else "",
                  int(o.get("reads", 0)), int(o.get("read_by", 0))) for o in objects])
            con.executemany(
                "INSERT OR IGNORE INTO dependency (version_id, source, target, kind, via_variable) VALUES (?,?,?,?,?)",
                [(version_id, d["source"], d["target"], d["kind"], int(bool(d.get("via_variable"))))
                 for d in dependencies])
            con.executemany(
                "INSERT OR IGNORE INTO visual (version_id, node, report, page, page_order, visual_type, title) "
                "VALUES (?,?,?,?,?,?,?)",
                [(version_id, v["node"], v.get("report", ""), v.get("page", ""), int(v.get("page_order", 0)),
                  v.get("visual_type", ""), v.get("title", "")) for v in visuals])
            con.executemany(
                "INSERT OR IGNORE INTO usage (version_id, visual, field, roles) VALUES (?,?,?,?)",
                [(version_id, u["visual"], u["field"], json.dumps(u.get("roles", []))) for u in usages])
            con.execute("UPDATE model SET current_version_id = ? WHERE id = ?", (version_id, model_id))
            return {"model_id": model_id, "version_id": version_id, "number": number}

    # -- reading ----------------------------------------------------------

    def summary(self) -> dict:
        with self.connect() as con:
            totals = dict(con.execute(
                "SELECT COUNT(*) AS models, "
                "COALESCE(SUM(v.measures), 0) AS measures, COALESCE(SUM(v.unused_measures), 0) AS unused_measures, "
                "COALESCE(SUM(v.visuals), 0) AS visuals, COALESCE(SUM(v.broken_refs), 0) AS broken_refs, "
                "COALESCE(SUM(CASE WHEN v.status != 'ok' THEN 1 ELSE 0 END), 0) AS with_problems "
                "FROM model m LEFT JOIN model_version v ON v.id = m.current_version_id WHERE m.missing = 0"
            ).fetchone())
            totals["decisions"] = dict(con.execute(
                "SELECT COALESCE(SUM(CASE WHEN decision = 'keep' THEN 1 ELSE 0 END), 0) AS keep, "
                "COALESCE(SUM(CASE WHEN decision = 'remove' THEN 1 ELSE 0 END), 0) AS remove FROM annotation"
            ).fetchone())
        totals["folders"] = self.folders()
        totals["last_run"] = self.last_run()
        return totals

    def models(self) -> list:
        with self.connect() as con:
            return _rows(con.execute(
                "SELECT m.id, m.name, m.path, m.kind, m.owner, m.team, m.notes, m.missing, m.first_seen_at, "
                "m.last_seen_at, v.id AS version_id, v.number AS version, v.status, v.note, v.file_modified_at, "
                "v.scanned_at, v.measures, v.columns, v.tables, v.visuals, v.pages, v.unused_measures, "
                "v.unused_columns, v.broken_refs, v.has_report, "
                "(SELECT COUNT(*) FROM model_version x WHERE x.model_id = m.id) AS versions "
                "FROM model m LEFT JOIN model_version v ON v.id = m.current_version_id "
                "ORDER BY m.missing, fold(m.name)"))

    def model(self, model_id: int) -> Optional[dict]:
        found = [m for m in self.models() if m["id"] == model_id]
        if not found:
            return None
        model = found[0]
        with self.connect() as con:
            model["history"] = _rows(con.execute(
                "SELECT id, number, file_hash, file_size, file_modified_at, scanned_at, status, note, measures, "
                "columns, visuals, unused_measures, broken_refs FROM model_version WHERE model_id = ? "
                "ORDER BY number DESC", (model_id,)))
            model["objects"] = self._objects(con, model["version_id"], model_id) if model["version_id"] else []
            model["visual_list"] = _rows(con.execute(
                "SELECT v.node, v.report, v.page, v.page_order, v.visual_type, v.title, "
                "(SELECT COUNT(*) FROM usage u WHERE u.version_id = v.version_id AND u.visual = v.node) AS fields "
                "FROM visual v WHERE v.version_id = ? ORDER BY v.page_order, v.page, v.title",
                (model["version_id"],))) if model["version_id"] else []
        return model

    def _objects(self, con, version_id: int, model_id: int) -> list:
        rows = _rows(con.execute(
            "SELECT o.id, o.node, o.object_key, o.kind, o.table_name, o.name, o.display_folder, o.is_hidden, "
            "o.is_calculated, o.state, o.depth, o.in_model, o.usage_status, o.reads, o.read_by, "
            "o.description != '' AS has_description, a.owner, a.note, a.decision "
            "FROM object o LEFT JOIN annotation a ON a.model_id = ? AND a.object_key = o.object_key "
            "WHERE o.version_id = ? ORDER BY o.kind, fold(o.table_name), fold(o.name)", (model_id, version_id)))
        for r in rows:
            r["owner"], r["note"], r["decision"] = r["owner"] or "", r["note"] or "", r["decision"] or ""
        return rows

    def object(self, object_id: int) -> Optional[dict]:
        with self.connect() as con:
            row = con.execute(
                "SELECT o.*, v.model_id, v.number AS version, v.status AS version_status, m.name AS model_name, "
                "m.kind AS model_kind, "
                "m.current_version_id = o.version_id AS is_current "
                "FROM object o JOIN model_version v ON v.id = o.version_id JOIN model m ON m.id = v.model_id "
                "WHERE o.id = ?", (object_id,)).fetchone()
            if row is None:
                return None
            obj = dict(row)
            obj["usage"] = json.loads(obj["usage"]) if obj["usage"] else None
            version_id, model_id = obj["version_id"], obj["model_id"]
            names = {r["node"]: r for r in _rows(con.execute(
                "SELECT id, node, kind, table_name, name, usage_status, in_model FROM object WHERE version_id = ?",
                (version_id,)))}

            def link(node_id: str) -> dict:
                o = names.get(node_id)
                return ({"id": o["id"], "kind": o["kind"], "table_name": o["table_name"], "name": o["name"],
                         "usage_status": o["usage_status"], "in_model": o["in_model"]} if o else
                        {"id": None, "kind": "", "table_name": "", "name": node_id, "usage_status": "", "in_model": 0})

            obj["reads_list"] = [dict(link(r["target"]), via_variable=r["via_variable"], edge=r["kind"]) for r in
                                 con.execute("SELECT target, kind, via_variable FROM dependency "
                                             "WHERE version_id = ? AND source = ?", (version_id, obj["node"]))]
            obj["read_by_list"] = [dict(link(r["source"]), edge=r["kind"]) for r in
                                   con.execute("SELECT source, kind FROM dependency WHERE version_id = ? AND target = ?",
                                               (version_id, obj["node"]))]
            visuals = {r["node"]: r for r in _rows(con.execute(
                "SELECT node, report, page, page_order, visual_type, title FROM visual WHERE version_id = ?",
                (version_id,)))}
            obj["visuals"] = [dict(visuals.get(r["visual"], {"node": r["visual"]}), roles=json.loads(r["roles"]))
                              for r in con.execute("SELECT visual, roles FROM usage WHERE version_id = ? AND field = ?",
                                                   (version_id, obj["node"]))]
            via = (obj["usage"] or {}).get("via", [])
            obj["via_visuals"] = [dict(visuals.get(v["visual"], {"node": v["visual"]}), through=link(v["through"]))
                                  for v in via]
            ann = con.execute("SELECT owner, note, decision, updated_at, updated_by FROM annotation "
                              "WHERE model_id = ? AND object_key = ?", (model_id, obj["object_key"])).fetchone()
            obj["annotation"] = dict(ann) if ann else {"owner": "", "note": "", "decision": "", "updated_at": "",
                                                       "updated_by": ""}
            obj["history"] = _rows(con.execute(
                "SELECT o.id, v.number AS version, v.file_modified_at, v.scanned_at, o.table_name, o.name, "
                "o.expression, o.usage_status FROM object o JOIN model_version v ON v.id = o.version_id "
                "WHERE v.model_id = ? AND o.object_key = ? ORDER BY v.number DESC", (model_id, obj["object_key"])))
            # same name in other models: the cross-model where-used
            obj["elsewhere"] = _rows(con.execute(
                "SELECT o.id, m.id AS model_id, m.name AS model_name, m.kind AS model_kind, o.kind, o.table_name, "
                "o.usage_status, o.in_model FROM object o JOIN model m ON m.current_version_id = o.version_id "
                "WHERE fold(o.name) = fold(?) AND o.kind = ? AND m.id != ? AND m.missing = 0 "
                "ORDER BY o.in_model DESC, fold(m.name)",
                (obj["name"], obj["kind"], model_id)))
            return obj

    def search(self, query: str, kinds=("measure", "column"), in_dax: bool = False, limit: int = 200) -> list:
        """Objects in the current version of every model whose name (or DAX) contains ``query``."""
        query = (query or "").strip()
        if not query:
            return []
        like = "%" + query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where = "(fold(o.name) LIKE ? ESCAPE '\\' OR fold(o.table_name || '[' || o.name || ']') LIKE ? ESCAPE '\\'"
        params = [like, like]
        if in_dax:
            where += " OR fold(o.expression) LIKE ? ESCAPE '\\'"
            params.append(like)
        where += ")"
        marks = ",".join("?" * len(kinds))
        with self.connect() as con:
            rows = _rows(con.execute(
                f"SELECT o.id, o.kind, o.table_name, o.name, o.usage_status, o.in_model, o.display_folder, "
                f"o.expression, m.id AS model_id, m.name AS model_name, m.kind AS model_kind, a.decision, a.owner "
                f"FROM object o JOIN model m ON m.current_version_id = o.version_id "
                f"LEFT JOIN annotation a ON a.model_id = m.id AND a.object_key = o.object_key "
                f"WHERE m.missing = 0 AND o.kind IN ({marks}) AND {where} "
                f"ORDER BY fold(o.name) = ? DESC, fold(o.name), o.in_model DESC, fold(m.name) LIMIT ?",
                (*kinds, *params, query.casefold(), limit)))
        for r in rows:
            expression = r.pop("expression") or ""
            r["dax_match"] = in_dax and query.casefold() in expression.casefold() \
                and query.casefold() not in r["name"].casefold()
            r["decision"], r["owner"] = r["decision"] or "", r["owner"] or ""
        return rows

    # -- the team's notes -------------------------------------------------

    def annotate(self, model_id: int, object_key: str, owner: str = "", note: str = "", decision: str = "",
                 by: str = "") -> dict:
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {', '.join(d or '(none)' for d in DECISIONS)}")
        owner, note, by = owner.strip()[:200], note.strip()[:4000], by.strip()[:200]
        with self.connect() as con:
            if con.execute("SELECT 1 FROM model WHERE id = ?", (model_id,)).fetchone() is None:
                raise KeyError("no such model")
            if not (owner or note or decision):
                con.execute("DELETE FROM annotation WHERE model_id = ? AND object_key = ?", (model_id, object_key))
            else:
                con.execute(
                    "INSERT INTO annotation (model_id, object_key, owner, note, decision, updated_at, updated_by) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT (model_id, object_key) DO UPDATE SET owner = excluded.owner, "
                    "note = excluded.note, decision = excluded.decision, updated_at = excluded.updated_at, "
                    "updated_by = excluded.updated_by",
                    (model_id, object_key, owner, note, decision, now(), by))
        return {"owner": owner, "note": note, "decision": decision, "updated_by": by}

    def describe_model(self, model_id: int, owner: Optional[str] = None, team: Optional[str] = None,
                       notes: Optional[str] = None) -> None:
        with self.connect() as con:
            if con.execute("SELECT 1 FROM model WHERE id = ?", (model_id,)).fetchone() is None:
                raise KeyError("no such model")
            for column, value, limit in (("owner", owner, 200), ("team", team, 200), ("notes", notes, 4000)):
                if value is not None:
                    con.execute(f"UPDATE model SET {column} = ? WHERE id = ?", (value.strip()[:limit], model_id))

    def decisions(self, decision: str = "remove") -> list:
        """Objects the team has marked, with where they are now: the cleanup list for milestone 4."""
        with self.connect() as con:
            return _rows(con.execute(
                "SELECT a.*, m.name AS model_name, o.id AS object_id, o.kind, o.table_name, o.name, o.usage_status "
                "FROM annotation a JOIN model m ON m.id = a.model_id "
                "LEFT JOIN object o ON o.version_id = m.current_version_id AND o.object_key = a.object_key "
                "WHERE a.decision = ? ORDER BY fold(m.name), fold(o.name)", (decision,)))
