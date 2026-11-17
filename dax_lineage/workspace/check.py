"""Run the standards on a version, store the findings and the score, and read them back.

A version is checked when it is scanned. When the team changes a rule or
accepts an exception, every model's current version is checked again, so the
score always reflects today's rules; older versions keep the score they had
under the rules of their day (``rules_hash`` tells which).
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from . import rules as R
from .store import Store, _rows, now


# -- the rule set -----------------------------------------------------------------

def rule_set(store: Store) -> list:
    """Every rule, with the team's settings over the defaults."""
    with store.connect() as con:
        saved = {r["rule_id"]: dict(r) for r in con.execute("SELECT * FROM rule_setting")}
    out = []
    for rule in R.RULES:
        item = rule.to_dict()
        if rule.id in saved:
            s = saved[rule.id]
            item.update(enabled=bool(s["enabled"]), severity=s["severity"],
                        params={**rule.params, **json.loads(s["params"] or "{}")},
                        updated_at=s["updated_at"], updated_by=s["updated_by"])
        item["default"] = {"enabled": rule.enabled, "severity": rule.severity, "params": dict(rule.params)}
        out.append(item)
    return out


def rules_hash(rules: list) -> str:
    key = json.dumps([(r["id"], r["enabled"], r["severity"], r["params"]) for r in rules], sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def set_rule(store: Store, rule_id: str, enabled: Optional[bool] = None, severity: Optional[str] = None,
             params: Optional[dict] = None, by: str = "") -> dict:
    if rule_id not in R.BY_ID:
        raise KeyError(f"no such rule: {rule_id}")
    current = next(r for r in rule_set(store) if r["id"] == rule_id)
    if severity is not None and severity not in R.SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(R.SEVERITIES)}")
    merged = dict(current["params"])
    for key, value in (params or {}).items():
        if key not in R.BY_ID[rule_id].params:
            raise ValueError(f"{rule_id} has no setting {key!r}")
        default = R.BY_ID[rule_id].params[key]
        if isinstance(default, int):
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a whole number") from None
        elif key == "pattern" and value:
            import re
            try:
                re.compile(str(value))
            except re.error as err:
                raise ValueError(f"not a valid regular expression: {err}") from None
        merged[key] = value if isinstance(default, int) else str(value)
    enabled = current["enabled"] if enabled is None else bool(enabled)
    severity = severity or current["severity"]
    with store.connect() as con:
        con.execute(
            "INSERT INTO rule_setting (rule_id, enabled, severity, params, updated_at, updated_by) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT (rule_id) DO UPDATE SET enabled = excluded.enabled, severity = excluded.severity, "
            "params = excluded.params, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (rule_id, int(enabled), severity, json.dumps(merged), now(), by.strip()[:200]))
    recheck(store)
    return next(r for r in rule_set(store) if r["id"] == rule_id)


def reset_rules(store: Store) -> None:
    with store.connect() as con:
        con.execute("DELETE FROM rule_setting")
    recheck(store)


def export_rules(store: Store) -> dict:
    """The team's rule set as JSON, to keep in git or share with another workspace."""
    return {"rules": {r["id"]: {"enabled": r["enabled"], "severity": r["severity"], "params": r["params"]}
                      for r in rule_set(store)}}


def import_rules(store: Store, data: dict, by: str = "") -> int:
    items = (data or {}).get("rules")
    if not isinstance(items, dict):
        raise ValueError("expected {\"rules\": {rule id: {enabled, severity, params}}}")
    unknown = [k for k in items if k not in R.BY_ID]
    if unknown:
        raise ValueError(f"unknown rules: {', '.join(unknown)}")
    for rule_id, s in items.items():
        set_rule(store, rule_id, s.get("enabled"), s.get("severity"), s.get("params"), by)
    return len(items)


# -- exceptions -------------------------------------------------------------------

def accept(store: Store, model_id: int, object_key: str, rule_id: str, reason: str, by: str = "") -> None:
    if rule_id not in R.BY_ID:
        raise KeyError(f"no such rule: {rule_id}")
    if not reason.strip():
        raise ValueError("say why this is acceptable")
    with store.connect() as con:
        if con.execute("SELECT 1 FROM model WHERE id = ?", (model_id,)).fetchone() is None:
            raise KeyError("no such model")
        con.execute(
            "INSERT INTO rule_exception (model_id, object_key, rule_id, reason, created_by, created_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT (model_id, object_key, rule_id) DO UPDATE SET reason = excluded.reason, "
            "created_by = excluded.created_by, created_at = excluded.created_at",
            (model_id, object_key, rule_id, reason.strip()[:2000], by.strip()[:200], now()))
    recheck(store, [model_id])


def withdraw(store: Store, model_id: int, object_key: str, rule_id: str) -> bool:
    with store.connect() as con:
        gone = con.execute("DELETE FROM rule_exception WHERE model_id = ? AND object_key = ? AND rule_id = ?",
                           (model_id, object_key, rule_id)).rowcount > 0
    recheck(store, [model_id])
    return gone


def exceptions(store: Store, model_id: Optional[int] = None) -> list:
    with store.connect() as con:
        return _rows(con.execute(
            "SELECT e.*, m.name AS model_name FROM rule_exception e JOIN model m ON m.id = e.model_id "
            "WHERE ? IS NULL OR e.model_id = ? ORDER BY m.name, e.rule_id", (model_id, model_id)))


# -- checking ---------------------------------------------------------------------

def check_version(store: Store, version_id: int, rules: Optional[list] = None) -> Optional[dict]:
    version = store.version(version_id)
    # a report without its model, or a model this reader could not open, has nothing to hold to the rules
    if version is None or version["status"] != "ok" or version["model_kind"] != "model":
        with store.connect() as con:
            con.execute("DELETE FROM finding WHERE version_id = ?", (version_id,))
            con.execute("DELETE FROM version_check WHERE version_id = ?", (version_id,))
        return None
    rules = rules or rule_set(store)
    rows = store.version_rows(version_id)
    results = R.run(rules, rows, bool(version["has_report"]))
    with store.connect() as con:
        accepted = {(r["rule_id"], r["object_key"]) for r in con.execute(
            "SELECT rule_id, object_key FROM rule_exception WHERE model_id = ?", (version["model_id"],))}
    summary = R.score(results, accepted)
    stamp = now()
    with store.connect() as con:
        con.execute("DELETE FROM finding WHERE version_id = ?", (version_id,))
        con.executemany(
            "INSERT INTO finding (version_id, rule_id, severity, object_key, node, kind, label, message, accepted) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [(version_id, rule["id"], rule["severity"], o["object_key"], o["node"], o["kind"], R.label(o), message,
              int((rule["id"], o["object_key"]) in accepted))
             for rule, _, found in results for o, message in found])
        con.execute(
            "INSERT INTO version_check (version_id, score, errors, warnings, infos, accepted, rules, rules_hash, "
            "checked_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (version_id) DO UPDATE SET score = excluded.score, "
            "errors = excluded.errors, warnings = excluded.warnings, infos = excluded.infos, "
            "accepted = excluded.accepted, rules = excluded.rules, rules_hash = excluded.rules_hash, "
            "checked_at = excluded.checked_at",
            (version_id, summary["score"], summary["error"], summary["warning"], summary["info"], summary["accepted"],
             json.dumps(summary["rules"]), rules_hash(rules), stamp))
    return summary


def recheck(store: Store, model_ids: Optional[list] = None) -> int:
    """Check every model's current version again (after the rules or exceptions changed)."""
    rules = rule_set(store)
    count = 0
    for m in store.models():
        if m["version_id"] and (model_ids is None or m["id"] in model_ids):
            if check_version(store, m["version_id"], rules) is not None:
                count += 1
    return count


def ensure_checked(store: Store) -> int:
    """Check current versions that have never been checked, such as those scanned before milestone 3."""
    with store.connect() as con:
        todo = [r["id"] for r in con.execute(
            "SELECT v.id FROM model m JOIN model_version v ON v.id = m.current_version_id "
            "LEFT JOIN version_check c ON c.version_id = v.id WHERE c.version_id IS NULL AND v.status = 'ok' "
            "AND m.kind = 'model'")]
    rules = rule_set(store)
    for version_id in todo:
        check_version(store, version_id, rules)
    return len(todo)


# -- reading ----------------------------------------------------------------------

def version_check(store: Store, version_id: int) -> Optional[dict]:
    with store.connect() as con:
        row = con.execute("SELECT * FROM version_check WHERE version_id = ?", (version_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["rules"] = json.loads(out["rules"])
        out["findings"] = _rows(con.execute(
            "SELECT f.*, o.id AS object_id FROM finding f LEFT JOIN object o ON o.version_id = f.version_id "
            "AND o.node = f.node WHERE f.version_id = ? ORDER BY CASE f.severity WHEN 'error' THEN 0 "
            "WHEN 'warning' THEN 1 ELSE 2 END, f.rule_id, f.label", (version_id,)))
    out["stale"] = out["rules_hash"] != rules_hash(rule_set(store))
    return out


def findings_for(store: Store, version_id: int, object_key: str) -> list:
    with store.connect() as con:
        return _rows(con.execute(
            "SELECT f.*, e.reason, e.created_by, e.created_at FROM finding f "
            "JOIN model_version v ON v.id = f.version_id "
            "LEFT JOIN rule_exception e ON e.model_id = v.model_id AND e.object_key = f.object_key "
            "AND e.rule_id = f.rule_id WHERE f.version_id = ? AND f.object_key = ?", (version_id, object_key)))


def compare_checks(store: Store, version_id: int, against_id: Optional[int]) -> Optional[dict]:
    """Score change and findings new or fixed between two versions, for the review page."""
    new = version_check(store, version_id)
    if new is None and check_version(store, version_id) is not None:
        new = version_check(store, version_id)
    if new is None:
        return None
    old = version_check(store, against_id) if against_id else None
    if against_id and old is None and check_version(store, against_id) is not None:  # scanned before the rules existed
        old = version_check(store, against_id)
    key = lambda f: (f["rule_id"], f["object_key"])
    open_new = {key(f): f for f in new["findings"] if not f["accepted"]}
    open_old = {key(f): f for f in old["findings"] if not f["accepted"]} if old else {}
    return {
        "score": new["score"], "previous": old["score"] if old else None,
        "previous_stale": bool(old and old["rules_hash"] != new["rules_hash"]),
        "new": [f for k, f in open_new.items() if k not in open_old] if old else [],
        "fixed": [f for k, f in open_old.items() if k not in open_new],
    }


def standards(store: Store) -> dict:
    """The standards page: rules with counts across models, model scores, and findings per owner."""
    ensure_checked(store)
    rule_list = rule_set(store)
    with store.connect() as con:
        models = _rows(con.execute(
            "SELECT m.id, m.name, m.owner, m.team, m.kind, v.id AS version_id, v.number, c.score, c.errors, "
            "c.warnings, c.infos, c.accepted, "
            "(SELECT pc.score FROM model_version p JOIN version_check pc ON pc.version_id = p.id "
            " WHERE p.model_id = m.id AND p.number < v.number ORDER BY p.number DESC LIMIT 1) AS previous_score "
            "FROM model m JOIN model_version v ON v.id = m.current_version_id "
            "JOIN version_check c ON c.version_id = v.id WHERE m.missing = 0 ORDER BY c.score, m.name"))
        counts = {r["rule_id"]: dict(r) for r in con.execute(
            "SELECT f.rule_id, SUM(1 - f.accepted) AS open, SUM(f.accepted) AS accepted, "
            "COUNT(DISTINCT m.id) AS models FROM finding f JOIN model m ON m.current_version_id = f.version_id "
            "WHERE m.missing = 0 GROUP BY f.rule_id")}
        owners = _rows(con.execute(
            "SELECT COALESCE(NULLIF(a.owner, ''), NULLIF(m.owner, ''), '') AS owner, "
            "SUM(CASE WHEN f.severity = 'error' THEN 1 ELSE 0 END) AS errors, "
            "SUM(CASE WHEN f.severity = 'warning' THEN 1 ELSE 0 END) AS warnings, "
            "SUM(CASE WHEN f.severity = 'info' THEN 1 ELSE 0 END) AS infos, COUNT(*) AS total, "
            "COUNT(DISTINCT m.id) AS models "
            "FROM finding f JOIN model m ON m.current_version_id = f.version_id "
            "LEFT JOIN annotation a ON a.model_id = m.id AND a.object_key = f.object_key "
            "WHERE m.missing = 0 AND f.accepted = 0 GROUP BY 1 ORDER BY errors DESC, total DESC"))
    for r in rule_list:
        c = counts.get(r["id"], {})
        r.update(open=c.get("open") or 0, accepted=c.get("accepted") or 0, models=c.get("models") or 0)
    average = round(sum(m["score"] for m in models) / len(models)) if models else None
    return {"rules": rule_list, "models": models, "owners": owners, "average": average,
            "exceptions": exceptions(store)}


def owner_findings(store: Store, owner: str) -> list:
    """Open findings in current versions that belong to ``owner`` ('' for nobody)."""
    with store.connect() as con:
        return _rows(con.execute(
            "SELECT f.rule_id, f.severity, f.label, f.message, f.kind, o.id AS object_id, m.id AS model_id, "
            "m.name AS model_name FROM finding f JOIN model m ON m.current_version_id = f.version_id "
            "LEFT JOIN annotation a ON a.model_id = m.id AND a.object_key = f.object_key "
            "LEFT JOIN object o ON o.version_id = f.version_id AND o.node = f.node "
            "WHERE m.missing = 0 AND f.accepted = 0 AND COALESCE(NULLIF(a.owner, ''), NULLIF(m.owner, ''), '') = ? "
            "ORDER BY CASE f.severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, m.name, f.label",
            (owner,)))
