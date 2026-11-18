"""The metric glossary: one agreed definition per metric, and every copy of it across files.

A team that builds self-contained .pbix files copies its key measures from
file to file, and the copies drift: someone fixes Net Sales in one report and
not in the other nine. A metric pins the agreed version:

- it starts as a **draft** made from one measure in one model, with a business
  definition and an owner;
- a reviewer **certifies** it: its DAX at that moment becomes the agreed DAX;
- every measure with the metric's name (or one of its aliases), in the current
  version of every model, is then one of
    defines   the measure the metric was made from
    same      the same DAX (spacing, case and comments aside)
    variant   different on purpose: a reviewer accepted it, with a reason
    drift     different, and nobody said why;
- when the defining measure itself changes in a later save, the metric shows
  the change until a reviewer **re-certifies** it (or it is put right);
- a metric no longer wanted is **deprecated**: its copies are then flagged as
  using a retired definition.

Drift and deprecated copies are findings of the ``metric-drift`` standards
rule, so they count in each model's score and land with each owner.
"""

from __future__ import annotations

import json
from typing import Optional

from .changes import meaning
from .store import Store, _rows, now

STATUSES = ("draft", "certified", "deprecated")


def _key(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def _names(metric: dict) -> set:
    aliases = metric["aliases"] if isinstance(metric["aliases"], list) else json.loads(metric["aliases"] or "[]")
    return {_key(metric["name"])} | {_key(a) for a in aliases if a.strip()}


def _row(row) -> dict:
    out = dict(row)
    out["aliases"] = json.loads(out["aliases"] or "[]")
    return out


def get(store: Store, metric_id: int) -> Optional[dict]:
    with store.connect() as con:
        row = con.execute("SELECT mt.*, m.name AS model_name FROM metric mt LEFT JOIN model m ON m.id = mt.model_id "
                          "WHERE mt.id = ?", (metric_id,)).fetchone()
    return _row(row) if row else None


def all_metrics(store: Store) -> list:
    with store.connect() as con:
        return [_row(r) for r in con.execute(
            "SELECT mt.*, m.name AS model_name FROM metric mt LEFT JOIN model m ON m.id = mt.model_id "
            "ORDER BY CASE mt.status WHEN 'certified' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END, fold(mt.name)")]


def _current_measure(store: Store, model_id: int, object_key: str) -> Optional[dict]:
    with store.connect() as con:
        row = con.execute(
            "SELECT o.id, o.name, o.table_name, o.expression, o.format_string, o.node FROM object o "
            "JOIN model m ON m.current_version_id = o.version_id WHERE m.id = ? AND o.object_key = ? "
            "AND o.kind = 'measure' AND o.in_model = 1", (model_id, object_key)).fetchone()
    return dict(row) if row else None


def _clean_aliases(aliases) -> list:
    if isinstance(aliases, str):
        aliases = [a for a in aliases.split(",")]
    return sorted({a.strip()[:100] for a in (aliases or []) if a and a.strip()}, key=str.casefold)


def _check_names(store: Store, names: set, ignore_id: Optional[int] = None) -> None:
    for other in all_metrics(store):
        if other["id"] != ignore_id and names & _names(other):
            raise ValueError(f"the metric {other['name']} already uses the name "
                             f"{sorted(names & _names(other))[0]}")


# -- making and changing a metric ------------------------------------------------------------

def create(store: Store, model_id: int, object_key: str, name: str = "", definition: str = "", owner: str = "",
           aliases=None, by: str = "") -> dict:
    """A draft metric defined by one measure in the current version of one model."""
    measure = _current_measure(store, model_id, object_key)
    if measure is None:
        raise KeyError("no such measure in the model's current version")
    name = (name or measure["name"]).strip()[:100]
    aliases = _clean_aliases(aliases)
    _check_names(store, {_key(name)} | {_key(a) for a in aliases})
    stamp = now()
    with store.connect() as con:
        metric_id = con.execute(
            "INSERT INTO metric (name, name_key, aliases, definition, owner, model_id, object_key, expression, "
            "format_string, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, _key(name), json.dumps(aliases), definition.strip()[:4000], owner.strip()[:200], model_id,
             object_key, measure["expression"], measure["format_string"] or "", by.strip()[:200], stamp, stamp)
        ).lastrowid
    return get(store, metric_id)


def update(store: Store, metric_id: int, name: Optional[str] = None, definition: Optional[str] = None,
           owner: Optional[str] = None, aliases=None) -> dict:
    metric = get(store, metric_id)
    if metric is None:
        raise KeyError("no such metric")
    new_name = name.strip()[:100] if name is not None and name.strip() else metric["name"]
    new_aliases = _clean_aliases(aliases) if aliases is not None else metric["aliases"]
    _check_names(store, {_key(new_name)} | {_key(a) for a in new_aliases}, ignore_id=metric_id)
    with store.connect() as con:
        con.execute("UPDATE metric SET name = ?, name_key = ?, aliases = ?, definition = ?, owner = ?, updated_at = ? "
                    "WHERE id = ?",
                    (new_name, _key(new_name), json.dumps(new_aliases),
                     metric["definition"] if definition is None else definition.strip()[:4000],
                     metric["owner"] if owner is None else owner.strip()[:200], now(), metric_id))
    _recheck(store)
    return get(store, metric_id)


def certify(store: Store, metric_id: int, by: str = "", note: str = "") -> dict:
    """Make the defining measure's current DAX the agreed one (also how a changed source is re-certified)."""
    metric = get(store, metric_id)
    if metric is None:
        raise KeyError("no such metric")
    measure = _current_measure(store, metric["model_id"], metric["object_key"]) if metric["model_id"] else None
    if measure is None:
        raise ValueError("the measure that defines this metric is no longer in its model: point the metric at "
                         "another measure first")
    if not metric["owner"]:
        raise ValueError("give the metric an owner before certifying it: they approve changes to it")
    with store.connect() as con:
        con.execute("UPDATE metric SET status = 'certified', expression = ?, format_string = ?, certified_by = ?, "
                    "certified_at = ?, note = ?, updated_at = ? WHERE id = ?",
                    (measure["expression"], measure["format_string"] or "", by.strip()[:200], now(),
                     note.strip()[:2000], now(), metric_id))
    _recheck(store)
    return get(store, metric_id)


def define_by(store: Store, metric_id: int, model_id: int, object_key: str) -> dict:
    """Point a metric at another measure (a certified one keeps its agreed DAX until re-certified)."""
    if get(store, metric_id) is None:
        raise KeyError("no such metric")
    if _current_measure(store, model_id, object_key) is None:
        raise KeyError("no such measure in the model's current version")
    with store.connect() as con:
        con.execute("UPDATE metric SET model_id = ?, object_key = ?, updated_at = ? WHERE id = ?",
                    (model_id, object_key, now(), metric_id))
        con.execute("DELETE FROM metric_variant WHERE metric_id = ? AND model_id = ? AND object_key = ?",
                    (metric_id, model_id, object_key))
    _recheck(store)
    return get(store, metric_id)


def deprecate(store: Store, metric_id: int, by: str = "", note: str = "") -> dict:
    if get(store, metric_id) is None:
        raise KeyError("no such metric")
    with store.connect() as con:
        con.execute("UPDATE metric SET status = 'deprecated', note = ?, updated_at = ? WHERE id = ?",
                    (f"deprecated by {by.strip() or 'someone'}" + (f": {note.strip()}" if note.strip() else ""),
                     now(), metric_id))
    _recheck(store)
    return get(store, metric_id)


def delete(store: Store, metric_id: int) -> None:
    metric = get(store, metric_id)
    if metric is None:
        raise KeyError("no such metric")
    if metric["status"] == "certified":
        raise ValueError("a certified metric is deprecated, not deleted, so its history stays")
    with store.connect() as con:
        con.execute("DELETE FROM metric_variant WHERE metric_id = ?", (metric_id,))
        con.execute("DELETE FROM metric WHERE id = ?", (metric_id,))
    _recheck(store)


def accept_variant(store: Store, metric_id: int, model_id: int, object_key: str, reason: str, by: str = "") -> None:
    if get(store, metric_id) is None:
        raise KeyError("no such metric")
    if not reason.strip():
        raise ValueError("say why this copy may differ")
    with store.connect() as con:
        con.execute("INSERT INTO metric_variant (metric_id, model_id, object_key, reason, created_by, created_at) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT (metric_id, model_id, object_key) DO UPDATE SET "
                    "reason = excluded.reason, created_by = excluded.created_by, created_at = excluded.created_at",
                    (metric_id, model_id, object_key, reason.strip()[:2000], by.strip()[:200], now()))
    _recheck(store, [model_id])


def withdraw_variant(store: Store, metric_id: int, model_id: int, object_key: str) -> bool:
    with store.connect() as con:
        gone = con.execute("DELETE FROM metric_variant WHERE metric_id = ? AND model_id = ? AND object_key = ?",
                           (metric_id, model_id, object_key)).rowcount > 0
    _recheck(store, [model_id])
    return gone


def _recheck(store: Store, model_ids=None) -> None:
    from .check import recheck
    recheck(store, model_ids)


# -- every copy, and how it compares --------------------------------------------------------

def _variants(store: Store) -> dict:
    with store.connect() as con:
        return {(r["metric_id"], r["model_id"], r["object_key"]): dict(r) for r in con.execute(
            "SELECT * FROM metric_variant")}


def classify(metric: dict, model_id: int, measure: dict, variants: dict) -> str:
    if metric["model_id"] == model_id and metric["object_key"] == measure["object_key"]:
        return "defines"
    if meaning(measure["expression"]) == meaning(metric["expression"]):
        return "same"
    if (metric["id"], model_id, measure["object_key"]) in variants:
        return "variant"
    return "drift"


def copies(store: Store, metric: dict) -> list:
    """Every measure named like the metric in the current version of every model, classified."""
    names = _names(metric)
    variants = _variants(store)
    with store.connect() as con:
        rows = _rows(con.execute(
            "SELECT o.id, o.object_key, o.name, o.table_name, o.expression, o.usage_status, o.node, "
            "m.id AS model_id, m.name AS model_name, m.owner AS model_owner FROM object o "
            "JOIN model m ON m.current_version_id = o.version_id "
            "WHERE o.kind = 'measure' AND o.in_model = 1 AND m.missing = 0 ORDER BY fold(m.name), o.id"))
    out = []
    for r in rows:
        if _key(r["name"]) not in names:
            continue
        r["status"] = classify(metric, r["model_id"], r, variants)
        variant = variants.get((metric["id"], r["model_id"], r["object_key"]))
        r["variant"] = variant if r["status"] == "variant" else None
        out.append(r)
    return sorted(out, key=lambda r: (["defines", "drift", "variant", "same"].index(r["status"]), r["model_name"]))


def source_state(store: Store, metric: dict) -> dict:
    """Whether the defining measure still says what was certified."""
    measure = _current_measure(store, metric["model_id"], metric["object_key"]) if metric["model_id"] else None
    if measure is None:
        return {"state": "gone", "expression": ""}
    if metric["status"] == "certified" and meaning(measure["expression"]) != meaning(metric["expression"]):
        return {"state": "changed", "expression": measure["expression"], "object_id": measure["id"]}
    return {"state": "ok", "expression": measure["expression"], "object_id": measure["id"]}


def detail(store: Store, metric_id: int) -> Optional[dict]:
    metric = get(store, metric_id)
    if metric is None:
        return None
    metric["copies"] = copies(store, metric)
    metric["source"] = source_state(store, metric)
    return metric


def glossary(store: Store) -> dict:
    metrics = []
    for metric in all_metrics(store):
        found = copies(store, metric)
        counts = {s: sum(1 for c in found if c["status"] == s) for s in ("defines", "same", "variant", "drift")}
        metric.update(counts=counts, models=len({c["model_id"] for c in found}),
                      source=source_state(store, metric)["state"])
        metric.pop("expression", None)
        metrics.append(metric)
    return {"metrics": metrics, "candidates": candidates(store, metrics)}


def candidates(store: Store, metrics: Optional[list] = None) -> list:
    """Measure names in two or more models that no metric covers yet: where a glossary starts."""
    covered = set()
    for metric in metrics if metrics is not None else all_metrics(store):
        covered |= _names(metric)
    groups: dict = {}
    with store.connect() as con:
        for r in con.execute(
                "SELECT o.id, o.name, o.expression, m.id AS model_id, m.name AS model_name FROM object o "
                "JOIN model m ON m.current_version_id = o.version_id WHERE o.kind = 'measure' AND o.in_model = 1 "
                "AND m.missing = 0 AND m.kind = 'model'"):
            if _key(r["name"]) not in covered:
                groups.setdefault(_key(r["name"]), []).append(dict(r))
    out = []
    for items in groups.values():
        models = {i["model_id"] for i in items}
        if len(models) < 2:
            continue
        versions = {json.dumps(meaning(i["expression"])) for i in items}
        out.append({"name": items[0]["name"], "models": len(models), "versions": len(versions),
                    "copies": [{"object_id": i["id"], "model_id": i["model_id"], "model_name": i["model_name"]}
                               for i in items]})
    return sorted(out, key=lambda c: (-c["versions"], -c["models"], c["name"].casefold()))


def for_object(store: Store, model_id: int, object_key: str, name: str) -> Optional[dict]:
    """The metric a measure belongs to, and how this copy compares; None if no metric has its name."""
    key = _key(name)
    variants = _variants(store)
    for metric in all_metrics(store):
        if key in _names(metric) or (metric["model_id"] == model_id and metric["object_key"] == object_key):
            measure = _current_measure(store, model_id, object_key)
            if measure is None:
                return None
            measure["object_key"] = object_key
            status = classify(metric, model_id, measure, variants)
            return {"id": metric["id"], "name": metric["name"], "status": metric["status"], "owner": metric["owner"],
                    "definition": metric["definition"], "copy": status, "expression": metric["expression"],
                    "variant": variants.get((metric["id"], model_id, object_key))}
    return None


# -- what the rules and approvals need ---------------------------------------------------------

def for_rules(store: Store, model_id: int) -> list:
    """Certified and deprecated metrics, shaped for the metric-drift rule in one model."""
    variants = _variants(store)
    out = []
    for metric in all_metrics(store):
        if metric["status"] == "draft":
            continue
        out.append({"id": metric["id"], "name": metric["name"], "names": _names(metric), "status": metric["status"],
                    "owner": metric["owner"], "meaning": meaning(metric["expression"]),
                    "defines": metric["object_key"] if metric["model_id"] == model_id else None,
                    "variants": {k for (mid, mod, k) in variants if mid == metric["id"] and mod == model_id}})
    return out


def certified_defined_by(store: Store, model_id: int, object_keys) -> list:
    """Certified metrics whose defining measure is one of ``object_keys`` in ``model_id``."""
    keys = set(object_keys)
    return [m for m in all_metrics(store)
            if m["status"] == "certified" and m["model_id"] == model_id and m["object_key"] in keys]


def is_owner(metric: dict, who: Optional[dict], name: str = "") -> bool:
    owner = _key(metric["owner"])
    candidates = {_key(name)}
    if who:
        candidates |= {_key(who.get("username", "")), _key(who.get("display_name", ""))}
        candidates |= {_key(who.get("username", "").split("\\")[-1])}
    return bool(owner) and owner in candidates
