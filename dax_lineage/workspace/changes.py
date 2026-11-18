"""Change requests: a reviewed set of edits to one model, applied in Power BI Desktop, confirmed by a scan.

A request is built from what the team asks for (remove these measures, give
these a description, rename this one). Building it resolves every edit against
the model's current version, records the value each property has now, works
out what else must change with it, and refuses what would break something.

Rules that keep it safe:
- A measure is removed only when nothing outside the request reads it and no
  visual in the file shows it.
- A rename rewrites every DAX expression that refers to the measure. Visuals,
  though, find their fields by name, and a rename made by an external tool
  leaves them pointing at the old name. So a measure that visuals show is not
  renamed by the companion: the request asks the author to rename it in
  Desktop's field list, which updates the visuals, and the scan confirms it.
- Every edit carries the value it expects to find. The companion skips an
  edit whose object has changed since the request was approved.

Lifecycle: proposed -> approved | rejected | cancelled; approved -> applied
(the companion reports back) -> confirmed (a scanned version shows every
edit). An approved request done entirely by hand is confirmed by the scan too.
"""

from __future__ import annotations

import json
from typing import Optional

from ..dax import tokenize
from .store import Store, _rows, now

FIELDS = {  # field -> (label, TOM property, kinds it applies to)
    "description": ("description", "Description", ("measure", "column")),
    "display_folder": ("display folder", "DisplayFolder", ("measure", "column")),
    "format_string": ("format string", "FormatString", ("measure", "column")),
    "is_hidden": ("hidden", "IsHidden", ("measure", "column")),
}
OPEN = ("proposed", "approved", "applied")
DECISIONS = ("approved", "rejected", "cancelled", "proposed")



class Blocked(ValueError):
    """The request would break something; ``problems`` says what."""

    def __init__(self, problems: list):
        super().__init__("; ".join(problems))
        self.problems = problems


def label(o: dict) -> str:
    if o["kind"] in ("measure", "table") or not o.get("table_name"):
        return o["name"]
    return f"{o['table_name']}[{o['name']}]"


def _value(o: dict, field: str) -> str:
    value = o.get(field)
    if field == "is_hidden":
        return "true" if value else "false"
    return value or ""


def rewrite_reference(expression: str, old: str, new: str, home_table: str) -> str:
    """``expression`` with every reference to measure ``old`` renamed to ``new``.

    [Old] and 'Home'[Old] are rewritten; Other[Old] is a column of another table and is left alone.
    """
    tokens = tokenize(expression or "")
    edits = []
    for i, t in enumerate(tokens):
        if t.kind != "bracket" or t.value.casefold() != old.casefold():
            continue
        prev = tokens[i - 1] if i else None
        qualified = prev is not None and prev.kind in ("table", "ident") and prev.end == t.start
        if qualified and prev.value.casefold() != home_table.casefold():
            continue
        edits.append((t.start, t.end))
    text = expression
    for start, end in reversed(edits):
        text = text[:start] + "[" + new.replace("]", "]]") + "]" + text[end:]
    return text


def meaning(text: str) -> list:
    try:
        return [(t.kind, t.value if t.kind == "string" else t.value.casefold())
                for t in tokenize(text or "") if t.kind != "comment"]
    except Exception:
        return [" ".join((text or "").split())]


# -- building a request --------------------------------------------------------------

def plan(store: Store, model_id: int, items: list) -> dict:
    """Resolve requested edits against the model's current version: the ops, what blocks them, the warnings."""
    model = next((m for m in store.models() if m["id"] == model_id), None)
    if model is None:
        raise KeyError("no such model")
    if not model["version_id"] or model["status"] != "ok" or model["kind"] != "model":
        raise ValueError("this file's model could not be read, so changes cannot be checked against it")
    rows = store.version_rows(model["version_id"])
    objects = {o["object_key"]: o for o in rows["objects"] if o["in_model"]}
    by_node = {o["node"]: o for o in rows["objects"]}
    readers: dict = {}
    for d in rows["dependencies"]:
        readers.setdefault(d["target"], []).append(d["source"])
    visuals = {v["node"]: v for v in rows["visuals"]}
    shown_in: dict = {}
    for u in rows["usages"]:
        v = visuals.get(u["visual"], {})
        shown_in.setdefault(u["field"], []).append(f"{v.get('title') or v.get('visual_type') or u['visual']} "
                                                   f"on {v.get('page', '?')}")
    with store.connect() as con:
        keeps = {r["object_key"]: r["updated_by"] for r in con.execute(
            "SELECT object_key, updated_by FROM annotation WHERE model_id = ? AND decision = 'keep'", (model_id,))}
        reports = _rows(con.execute(
            "SELECT DISTINCT m.name, fold(o.name) AS name_key FROM object o JOIN model m ON m.current_version_id = o.version_id "
            "WHERE m.kind = 'report' OR (o.in_model = 0 AND m.id != ?)", (model_id,)))

        in_flight = {}
        for r in con.execute(
                "SELECT o.object_key, r.id FROM change_op o JOIN change_request r ON r.id = o.request_id "
                "WHERE r.model_id = ? AND r.status IN ('proposed', 'approved', 'applied') AND o.op != 'expression'",
                (model_id,)):
            in_flight.setdefault(r["object_key"], set()).add(r["id"])

    from .metrics import certified_defined_by
    guarded = {m["object_key"]: m for m in certified_defined_by(store, model_id, objects)}

    def elsewhere(name: str) -> list:
        return sorted({r["name"] for r in reports if r["name_key"] == name.casefold()})

    ops, problems, warnings = [], [], []
    removing = {i["object_key"] for i in items if i.get("op") == "remove"}
    touched: dict = {}
    pending_expr: dict = {}  # reader node -> expression after earlier renames in this request

    def add(op, o, mode="companion", field="", old="", new="", reason=""):
        ops.append({"seq": len(ops) + 1, "op": op, "mode": mode, "object_key": o["object_key"],
                    "lineage_tag": o.get("lineage_tag") or (o["object_key"] if o["object_key"] != o["node"] else ""),
                    "kind": o["kind"], "table_name": o["table_name"], "name": o["name"], "field": field,
                    "old_value": old, "new_value": new, "reason": reason})

    for item in items:
        kind_of_edit = item.get("op")
        o = objects.get(str(item.get("object_key", "")))
        if o is None:
            problems.append(f"{item.get('object_key')}: not in the current version of {model['name']}")
            continue
        name = label(o)
        ident = kind_of_edit if kind_of_edit != "set" else f"set:{item.get('field')}"
        seen = touched.setdefault(o["object_key"], set())
        if ident in seen or "remove" in seen or (ident == "remove" and seen):
            problems.append(f"{name}: asked for twice, or removed and edited in one request")
            continue
        seen.add(ident)
        for other in sorted(in_flight.get(o["object_key"], ())):
            warnings.append(f"{name} is already in open change #{other}")

        metric = guarded.get(o["object_key"])
        if metric is not None and kind_of_edit == "remove":
            problems.append(f"{name} defines the certified metric {metric['name']}: point the metric at another "
                            f"measure, or deprecate it, before removing it")
            continue
        if metric is not None:
            warnings.append(f"{name} defines the certified metric {metric['name']}: its owner "
                            f"({metric['owner'] or 'none set'}) or an admin approves this change")

        if kind_of_edit == "remove":
            if o["kind"] != "measure":
                problems.append(f"{name}: only measures can be removed this way (columns come from the query)")
                continue
            outside = [by_node[r] for r in readers.get(o["node"], []) if r in by_node
                       and by_node[r]["object_key"] not in removing]
            if outside:
                problems.append(f"{name}: still read by {', '.join(label(r) for r in outside)}; remove those too, "
                                f"or change them first")
                continue
            if o["node"] in shown_in:
                problems.append(f"{name}: shown in {', '.join(shown_in[o['node']])}; take it out of those visuals first")
                continue
            if o["object_key"] in keeps:
                warnings.append(f"{name} is marked keep by {keeps[o['object_key']] or 'someone'}")
            for report in elsewhere(o["name"]):
                warnings.append(f"{report} uses a field named {o['name']}: if it is built on this model, it will break")
            add("remove", o, reason=item.get("reason", ""), old=o.get("expression", ""))

        elif kind_of_edit == "set":
            field = item.get("field")
            if field not in FIELDS or o["kind"] not in FIELDS[field][2]:
                problems.append(f"{name}: cannot set {field}")
                continue
            new = item.get("value")
            if field == "is_hidden":
                new = "true" if new in (True, "true", "1", 1) else "false"
            else:
                new = str(new if new is not None else "").strip()
                if len(new) > 4000:
                    problems.append(f"{name}: {FIELDS[field][0]} is too long")
                    continue
            old = _value(o, field)
            if old == new:
                continue  # nothing to do
            add("set", o, field=field, old=old, new=new)

        elif kind_of_edit == "rename":
            new = str(item.get("value") or "").strip()
            if o["kind"] != "measure":
                problems.append(f"{name}: only measures can be renamed this way")
                continue
            if not new or any(c in new for c in "\t\r\n"):
                problems.append(f"{name}: the new name is empty or has a line break")
                continue
            if new == o["name"]:
                continue
            clash = [x for x in objects.values() if x["kind"] in ("measure", "column") and x is not o
                     and x["name"].casefold() == new.casefold()]
            if clash:
                problems.append(f"{name}: {label(clash[0])} already has the name {new}")
                continue
            for report in elsewhere(o["name"]):
                warnings.append(f"{report} uses a field named {o['name']}: if it is built on this model, "
                                f"it will need the new name")
            if o["node"] in shown_in:
                add("rename", o, mode="desktop", old=o["name"], new=new,
                    reason=f"shown in {len(shown_in[o['node']])} visual(s): rename it in Desktop's Data pane, "
                           f"which updates the visuals and the DAX that uses it")
                continue
            add("rename", o, old=o["name"], new=new)
            for reader_node in readers.get(o["node"], []):
                reader = by_node.get(reader_node)
                if not reader or not reader["in_model"] or not reader.get("expression"):
                    continue
                before = pending_expr.get(reader_node, reader["expression"])
                after = rewrite_reference(before, o["name"], new, o["table_name"])
                if after != before:
                    pending_expr[reader_node] = after
        else:
            problems.append(f"{name}: unknown change {kind_of_edit!r}")

    for node, after in pending_expr.items():
        reader = by_node[node]
        if reader["object_key"] in removing:
            continue
        add("expression", reader, field="expression", old=reader["expression"], new=after,
            reason="follows the rename")
    if not ops and not problems:
        problems.append("nothing would change")
    return {"model": model, "version_id": model["version_id"], "ops": ops, "problems": problems,
            "warnings": sorted(set(warnings))}


def create(store: Store, model_id: int, title: str, items: list, by: str = "") -> dict:
    planned = plan(store, model_id, items)
    if planned["problems"]:
        raise Blocked(planned["problems"])
    title = (title or "").strip()[:200] or _title(planned["ops"])
    stamp = now()
    with store.connect() as con:
        request_id = con.execute(
            "INSERT INTO change_request (model_id, base_version_id, title, created_by, created_at, warnings) "
            "VALUES (?,?,?,?,?,?)", (model_id, planned["version_id"], title, by.strip()[:200], stamp,
                                     json.dumps(planned["warnings"]))).lastrowid
        con.executemany(
            "INSERT INTO change_op (request_id, seq, op, mode, object_key, lineage_tag, kind, table_name, name, field, "
            "old_value, new_value, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(request_id, o["seq"], o["op"], o["mode"], o["object_key"], o["lineage_tag"], o["kind"], o["table_name"],
              o["name"], o["field"], o["old_value"], o["new_value"], o["reason"]) for o in planned["ops"]])
        con.execute("INSERT INTO change_event (request_id, kind, author, body, created_at) VALUES (?,?,?,?,?)",
                    (request_id, "proposed", by.strip()[:200], "", stamp))
    return get(store, request_id)


def _title(ops: list) -> str:
    kinds = [o for o in ops if o["op"] != "expression"]
    removes = [o for o in kinds if o["op"] == "remove"]
    renames = [o for o in kinds if o["op"] == "rename"]
    sets = [o for o in kinds if o["op"] == "set"]
    parts = []
    if removes:
        parts.append(f"remove {len(removes)} measure{'s' if len(removes) != 1 else ''}")
    if renames:
        parts.append("rename " + ", ".join(o["name"] for o in renames[:3]) + ("…" if len(renames) > 3 else ""))
    if sets:
        fields = sorted({FIELDS[o["field"]][0] for o in sets})
        parts.append(f"set {' and '.join(fields)} on {len({o['object_key'] for o in sets})} objects")
    text = "; ".join(parts) or "change"
    return text[0].upper() + text[1:]


# -- reading -------------------------------------------------------------------------

def get(store: Store, request_id: int) -> Optional[dict]:
    with store.connect() as con:
        row = con.execute(
            "SELECT r.*, m.name AS model_name, m.current_version_id, b.number AS base_number, "
            "cv.number AS current_number, conf.number AS confirmed_number "
            "FROM change_request r JOIN model m ON m.id = r.model_id "
            "JOIN model_version b ON b.id = r.base_version_id "
            "LEFT JOIN model_version cv ON cv.id = m.current_version_id "
            "LEFT JOIN model_version conf ON conf.id = r.confirmed_version_id WHERE r.id = ?", (request_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["warnings"] = json.loads(out["warnings"])
        out["ops"] = _rows(con.execute("SELECT * FROM change_op WHERE request_id = ? ORDER BY seq", (request_id,)))
        out["events"] = _rows(con.execute("SELECT * FROM change_event WHERE request_id = ? ORDER BY id", (request_id,)))
    out["stale"] = _stale(store, out) if out["status"] in ("proposed", "approved") else []
    out["manual"] = [o for o in out["ops"] if o["mode"] == "desktop"]
    return out


def _stale(store: Store, request: dict) -> list:
    """Edits whose object no longer has the value the request expects (the model changed since)."""
    if request["current_version_id"] == request["base_version_id"] or not request["current_version_id"]:
        return []
    rows = store.version_rows(request["current_version_id"])
    objects = {o["object_key"]: o for o in rows["objects"] if o["in_model"]}
    out = []
    for op in request["ops"]:
        o = objects.get(op["object_key"])
        if o is None:
            out.append({"seq": op["seq"], "why": f"{op['name']} is no longer in the model"})
        elif op["op"] == "set" and _value(o, op["field"]) != op["old_value"]:
            out.append({"seq": op["seq"], "why": f"{label(o)}: {FIELDS[op['field']][0]} changed since"})
        elif op["op"] in ("rename", "remove") and o["name"] != op["name"]:
            out.append({"seq": op["seq"], "why": f"{op['name']} was renamed to {o['name']} since"})
        elif op["op"] == "expression" and meaning(o["expression"]) != meaning(op["old_value"]):
            out.append({"seq": op["seq"], "why": f"{label(o)}: DAX changed since"})
    return out


def requests(store: Store, status: str = "open", model_id: Optional[int] = None) -> list:
    wanted = OPEN if status == "open" else (status,) if status != "all" else None
    with store.connect() as con:
        rows = _rows(con.execute(
            "SELECT r.id, r.model_id, r.title, r.status, r.created_by, r.created_at, r.decided_by, r.decided_at, "
            "r.applied_at, m.name AS model_name, (SELECT COUNT(*) FROM change_op o WHERE o.request_id = r.id "
            "AND o.op != 'expression') AS edits, (SELECT COUNT(*) FROM change_op o WHERE o.request_id = r.id "
            "AND o.mode = 'desktop') AS manual FROM change_request r JOIN model m ON m.id = r.model_id "
            "WHERE (? IS NULL OR r.model_id = ?) ORDER BY r.id DESC", (model_id, model_id)))
    return [r for r in rows if wanted is None or r["status"] in wanted]


# -- deciding, applying, confirming -----------------------------------------------------

def _event(con, request_id: int, kind: str, by: str, body: str = "") -> None:
    con.execute("INSERT INTO change_event (request_id, kind, author, body, created_at) VALUES (?,?,?,?,?)",
                (request_id, kind, by.strip()[:200], body.strip()[:4000], now()))


def decide(store: Store, request_id: int, status: str, by: str = "", note: str = "") -> dict:
    if status not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    request = get(store, request_id)
    if request is None:
        raise KeyError("no such change")
    allowed = {"approved": ("proposed",), "rejected": ("proposed",), "proposed": ("approved", "rejected"),
               "cancelled": ("proposed", "approved")}[status]
    if request["status"] not in allowed:
        raise ValueError(f"a change that is {request['status']} cannot become {status}")
    if status == "approved" and request["stale"]:
        raise ValueError("the model changed since this was proposed: " + "; ".join(s["why"] for s in request["stale"]))
    with store.connect() as con:
        con.execute("UPDATE change_request SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                    (status, by.strip()[:200] if status != "proposed" else "", now() if status != "proposed" else "",
                     request_id))
        _event(con, request_id, status, by, note)
    return get(store, request_id)


def comment(store: Store, request_id: int, body: str, by: str = "") -> dict:
    if not body.strip():
        raise ValueError("write something first")
    if get(store, request_id) is None:
        raise KeyError("no such change")
    with store.connect() as con:
        _event(con, request_id, "comment", by, body)
    return get(store, request_id)


def ready(store: Store) -> list:
    """Approved requests, shaped for the companion."""
    out = []
    for r in requests(store, "approved"):
        full = get(store, r["id"])
        out.append({
            "id": full["id"], "model": full["model_name"], "title": full["title"], "approved_by": full["decided_by"],
            "warnings": full["warnings"],
            "ops": [{"seq": o["seq"], "op": o["op"], "mode": o["mode"], "kind": o["kind"], "table": o["table_name"],
                     "name": o["name"], "lineage_tag": o["lineage_tag"],
                     "property": "Name" if o["op"] == "rename" else "Expression" if o["op"] == "expression"
                     else FIELDS[o["field"]][1] if o["op"] == "set" else "",
                     "old": o["old_value"], "new": o["new_value"], "reason": o["reason"]} for o in full["ops"]],
        })
    return out


RESULTS = ("applied", "already", "skipped", "failed", "manual")


def applied(store: Store, request_id: int, results: list, by: str = "", machine: str = "") -> dict:
    """The companion's report: what it did to each edit."""
    request = get(store, request_id)
    if request is None:
        raise KeyError("no such change")
    if request["status"] != "approved":
        raise ValueError(f"only an approved change can be applied; this one is {request['status']}")
    seqs = {o["seq"] for o in request["ops"]}
    clean = []
    for r in results or []:
        try:
            seq = int(r.get("seq"))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("each result needs a seq") from None
        if seq not in seqs:
            raise ValueError(f"no edit {seq} in this change")
        status = str(r.get("status", ""))
        if status not in RESULTS:
            raise ValueError(f"result must be one of {', '.join(RESULTS)}")
        clean.append((status, str(r.get("message", ""))[:1000], request_id, seq))
    done = any(s in ("applied", "already") for s, *_ in clean)
    with store.connect() as con:
        con.executemany("UPDATE change_op SET status = ?, message = ? WHERE request_id = ? AND seq = ?", clean)
        con.execute("UPDATE change_request SET status = ?, applied_by = ?, applied_at = ? WHERE id = ?",
                    ("applied" if done else "approved", by.strip()[:200], now() if done else "", request_id))
        counts = {s: sum(1 for x, *_ in clean if x == s) for s in RESULTS}
        summary = ", ".join(f"{n} {s}" for s, n in counts.items() if n)
        _event(con, request_id, "applied" if done else "apply-failed", by,
               f"{summary}" + (f" on {machine.strip()[:100]}" if machine.strip() else ""))
    return get(store, request_id)


def verify(store: Store, model_id: int, version_id: int) -> list:
    """After a scan: which open requests on this model the new version shows done. Returns confirmed ids."""
    rows = store.version_rows(version_id)
    objects = {o["object_key"]: o for o in rows["objects"] if o["in_model"]}
    confirmed = []
    with store.connect() as con:
        open_requests = [r["id"] for r in con.execute(
            "SELECT id FROM change_request WHERE model_id = ? AND status IN ('approved', 'applied') "
            "AND base_version_id < ?", (model_id, version_id))]
    for request_id in open_requests:
        request = get(store, request_id)
        results = []
        for op in request["ops"]:
            o = objects.get(op["object_key"])
            if op["op"] == "remove":
                ok = o is None
            elif o is None:
                ok = False
            elif op["op"] == "set":
                ok = _value(o, op["field"]) == op["new_value"]
            elif op["op"] == "rename":
                ok = o["name"] == op["new_value"]
            else:
                ok = meaning(o["expression"]) == meaning(op["new_value"])
            results.append((op["seq"], ok))
        found = sum(1 for _, ok in results if ok)
        with store.connect() as con:
            con.executemany("UPDATE change_op SET status = ? WHERE request_id = ? AND seq = ? AND status != ?",
                            [("confirmed" if ok else op_status, request_id, seq, "confirmed")
                             for (seq, ok), op_status in zip(results, [o["status"] for o in request["ops"]])])
            if found == len(results):
                con.execute("UPDATE change_request SET status = 'confirmed', confirmed_version_id = ? WHERE id = ?",
                            (version_id, request_id))
                _event(con, request_id, "confirmed", "scan", "every edit is in the saved file")
                confirmed.append(request_id)
            elif found and request["status"] == "applied":
                _event(con, request_id, "partly", "scan", f"{found} of {len(results)} edits are in the saved file")
    return confirmed
