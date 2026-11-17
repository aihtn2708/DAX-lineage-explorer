"""What changed between two versions of a model: the content of a review.

Objects are paired by ``object_key`` (the LineageTag), so a rename is a rename,
not a removal plus an addition. For a model without tags, a removed and an
added object of the same kind, in the same table, with the same DAX, are
paired as a probable rename.

Fields a visual reads are compared by the object they point at, so renaming a
measure does not make every visual that shows it look changed.
"""

from __future__ import annotations

import difflib
import re
from typing import Optional

from ..dax import tokenize
from .store import Store

FIELDS = (  # (column, label) compared for an object that exists in both versions
    ("table_name", "home table"),
    ("expression", "DAX"),
    ("description", "description"),
    ("display_folder", "display folder"),
    ("format_string", "format string"),
    ("data_type", "data type"),
    ("is_hidden", "hidden"),
    ("state", "state"),
)
UNUSED = ("unused", "deadChain")


def label(o: dict) -> str:
    if o["kind"] in ("measure", "table") or not o.get("table_name"):
        return o["name"]
    return f"{o['table_name']}[{o['name']}]"


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _meaning(text: str, renames: Optional[dict] = None) -> list:
    """The tokens that change what an expression does: no whitespace, no comments, names case-folded.

    ``renames`` maps old names to new ones ({"bracket": {...}, "table": {...}}, case-folded), so an
    expression that only follows a rename means the same as before.
    """
    try:
        tokens = tokenize(text or "")
    except Exception:  # an expression the tokenizer cannot follow is compared as text
        return [_squash(text)]
    renames = renames or {}
    out = []
    for t in tokens:
        if t.kind == "comment":
            continue
        value = t.value if t.kind == "string" else t.value.casefold()
        if t.kind == "bracket":
            value = renames.get("bracket", {}).get(value, value)
        elif t.kind in ("table", "ident"):
            value = renames.get("table", {}).get(value, value)
        out.append(("table" if t.kind == "ident" and value in renames.get("table_names", ()) else t.kind, value))
    return out


def dax_diff(old: str, new: str) -> dict:
    """Line diff of two expressions, and whether only whitespace changed."""
    a, b = (old or "").splitlines(), (new or "").splitlines()
    lines = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            lines += [{"op": " ", "text": t} for t in a[i1:i2]]
        else:
            lines += [{"op": "-", "text": t} for t in a[i1:i2]]
            lines += [{"op": "+", "text": t} for t in b[j1:j2]]
    return {"lines": lines, "formatting_only": _meaning(old) == _meaning(new)}


def _impact(o: dict) -> int:
    usage = o.get("usage") or {}
    return len(usage.get("visuals", [])) + len(usage.get("via", []))


def _pair_untagged(removed: list, added: list) -> list:
    """(old, new) pairs among objects without lineage tags that look like the same object renamed."""
    pairs = []
    for old in list(removed):
        if old["object_key"] != old["node"]:
            continue  # it had a real tag: a missing partner means it really went
        matches = [new for new in added if new["object_key"] == new["node"] and new["kind"] == old["kind"]
                   and new["table_name"] == old["table_name"] and _squash(new["expression"]) == _squash(old["expression"])
                   and (old["kind"] != "measure" or new["expression"])]
        if len(matches) == 1:
            pairs.append((old, matches[0]))
            removed.remove(old)
            added.remove(matches[0])
    return pairs


def compare(store: Store, version_id: int, against_id: Optional[int] = None) -> dict:
    """The review of ``version_id``: what changed since ``against_id`` (default: the previous readable version)."""
    new_v = store.version(version_id)
    if new_v is None:
        raise KeyError("no such version")
    old_v = store.version(against_id) if against_id else store.previous_version(version_id)
    if against_id and old_v is None:
        raise KeyError("no such version to compare with")
    if old_v is not None and old_v["model_id"] != new_v["model_id"]:
        raise ValueError("both versions must belong to the same model")
    result = {"version": new_v, "against": old_v, "baseline": old_v is None}
    if old_v is None or new_v["status"] == "error":
        result.update(counts={}, added=[], removed=[], renamed=[], edited=[], usage=[], broken=[], fixed=[],
                      visuals={"added": [], "removed": [], "changed": []})
        return result

    old, new = store.version_rows(old_v["id"]), store.version_rows(new_v["id"])
    old_in = {o["object_key"]: o for o in old["objects"] if o["in_model"]}
    new_in = {o["object_key"]: o for o in new["objects"] if o["in_model"]}

    removed = [o for k, o in old_in.items() if k not in new_in]
    added = [o for k, o in new_in.items() if k not in old_in]
    pairs = [(old_in[k], new_in[k]) for k in old_in if k in new_in]
    guessed = _pair_untagged(removed, added)
    pairs += guessed
    guessed_keys = {n["object_key"] for _, n in guessed}

    def reads(rows, node):
        return rows.get(node, set())

    def reads_by_source(version_rows, renamed_to):
        by_node = {o["node"]: o for o in version_rows["objects"]}
        out: dict = {}
        for d in version_rows["dependencies"]:
            target = by_node.get(d["target"])
            name = renamed_to.get(d["target"]) or (label(target) if target else d["target"])
            out.setdefault(d["source"], set()).add(name)
        return out

    # what an old object reads is named as it is now, so a rename is not "reads something else"
    old_reads = reads_by_source(old, {o["node"]: label(n) for o, n in pairs})
    new_reads = reads_by_source(new, {})

    renamed, edited, usage = [], [], []
    for o, n in pairs:
        item = {"key": n["object_key"], "id": n["id"], "kind": n["kind"], "label": label(n), "old_label": label(o),
                "impact": _impact(n), "usage_status": n["usage_status"]}
        if o["name"] != n["name"]:
            renamed.append(dict(item, guessed=n["object_key"] in guessed_keys))
        # a column's table changes only when its table is renamed, which is listed once, as the table
        changes = [name for column, name in FIELDS if column != "table_name" and o[column] != n[column]]
        if n["kind"] == "measure" and o["table_name"] != n["table_name"]:
            changes.insert(0, "home table")
        if changes:
            entry = dict(item, changes=changes,
                         before={column: o[column] for column, _ in FIELDS},
                         after={column: n[column] for column, _ in FIELDS})
            if "DAX" in changes:
                entry["dax"] = dax_diff(o["expression"], n["expression"])
                was, now_ = reads(old_reads, o["node"]), reads(new_reads, n["node"])
                entry["reads_added"], entry["reads_removed"] = sorted(now_ - was), sorted(was - now_)
            edited.append(entry)
        if o["usage_status"] != n["usage_status"] and n["kind"] in ("measure", "column") \
                and (o["usage_status"] in UNUSED) != (n["usage_status"] in UNUSED):
            usage.append(dict(item, before=o["usage_status"], after=n["usage_status"],
                              became_unused=n["usage_status"] in UNUSED))

    # expressions that changed only because something they read was renamed
    names = {"bracket": {}, "table": {}}
    for o, n in pairs:
        if o["name"] != n["name"]:
            names["table" if n["kind"] == "table" else "bracket"][o["name"].casefold()] = n["name"].casefold()
    names["table_names"] = set(names["table"].values())
    for entry in edited:
        if "DAX" in entry["changes"] and not entry["dax"]["formatting_only"] and (names["bracket"] or names["table"]):
            entry["follows_rename"] = _meaning(entry["before"]["expression"], names) == _meaning(entry["after"]["expression"], names)

    def brief(o):
        return {"key": o["object_key"], "id": o.get("id"), "kind": o["kind"], "label": label(o),
                "expression": o["expression"], "usage_status": o["usage_status"], "impact": _impact(o)}

    # references to objects the model does not have: new ones are a break, gone ones a fix
    def missing(rows):
        by_node = {o["node"]: o for o in rows["objects"]}
        readers: dict = {}
        for d in rows["dependencies"]:
            if d["target"] in by_node and not by_node[d["target"]]["in_model"]:
                src = by_node.get(d["source"])
                readers.setdefault(d["target"], set()).add(label(src) if src else d["source"])
        visuals = {v["node"]: v for v in rows["visuals"]}
        for u in rows["usages"]:
            if u["field"] in by_node and not by_node[u["field"]]["in_model"]:
                v = visuals.get(u["visual"], {})
                readers.setdefault(u["field"], set()).add(f"visual {v.get('title') or v.get('visual_type') or u['visual']}"
                                                         f" on {v.get('page', '?')}")
        return {label(by_node[k]): sorted(r) for k, r in readers.items()}

    was_missing, now_missing = missing(old), missing(new)
    broken = [{"label": k, "used_by": v} for k, v in sorted(now_missing.items()) if k not in was_missing]
    fixed = [{"label": k, "used_by": v} for k, v in sorted(was_missing.items()) if k not in now_missing]

    result.update(
        added=sorted((brief(o) for o in added), key=lambda x: (x["kind"], x["label"].casefold())),
        removed=sorted((brief(o) for o in removed), key=lambda x: (x["kind"], x["label"].casefold())),
        renamed=sorted(renamed, key=lambda x: x["label"].casefold()),
        edited=sorted(edited, key=lambda x: (x["kind"] != "measure", "DAX" not in x["changes"], x["label"].casefold())),
        usage=sorted(usage, key=lambda x: (not x["became_unused"], x["label"].casefold())),
        broken=broken, fixed=fixed,
        visuals=_visual_changes(old, new, {k for k in old_in if k in new_in} | {n["object_key"] for _, n in guessed},
                                {o["object_key"]: n["object_key"] for o, n in guessed}))
    v = result["visuals"]
    result["counts"] = {
        "added": len(result["added"]), "removed": len(result["removed"]), "renamed": len(renamed),
        "edited": len(edited), "dax": len([e for e in edited if "DAX" in e["changes"]
                                           and not e["dax"]["formatting_only"] and not e.get("follows_rename")]),
        "follow_renames": len([e for e in edited if e.get("follows_rename")]),
        "usage": len(usage), "broken": len(broken), "fixed": len(fixed),
        "formatting": len([e for e in edited if e.get("dax", {}).get("formatting_only")]),
        "visuals": len(v["added"]) + len(v["removed"]) + len(v["changed"]),
    }
    c = result["counts"]
    c["total"] = (c["added"] + c["removed"] + c["renamed"] + c["edited"] - c["follow_renames"] + c["broken"]
                  + c["fixed"] + c["visuals"])
    return result


def _visual_changes(old: dict, new: dict, common: set, translate: dict) -> dict:
    """Visuals added, removed, and changed in the fields they read.

    A field is the same field when it is the same object in both versions (``common`` keys, with
    ``translate`` for untagged renames); a field that is missing from a model is matched by name.
    """
    def fields(rows, translate_keys):
        by_node = {o["node"]: o for o in rows["objects"]}
        out: dict = {}
        for u in rows["usages"]:
            o = by_node.get(u["field"])
            name = label(o) if o else u["field"]
            key = translate_keys.get(o["object_key"], o["object_key"]) if o else ""
            ident = key if key in common else "name:" + name.casefold()
            out.setdefault(u["visual"], {})[ident] = (name, tuple(sorted(u["roles"])))
        return out

    old_vis, new_vis = {v["node"]: v for v in old["visuals"]}, {v["node"]: v for v in new["visuals"]}
    old_f, new_f = fields(old, translate), fields(new, {})

    def brief(v):
        return {"node": v["node"], "page": v["page"], "title": v["title"] or v["visual_type"], "type": v["visual_type"]}

    changed = []
    for node in sorted(set(old_vis) & set(new_vis), key=lambda n: (new_vis[n]["page_order"], new_vis[n]["title"])):
        a, b = old_f.get(node, {}), new_f.get(node, {})
        gained = [b[k][0] for k in b if k not in a]
        lost = [a[k][0] for k in a if k not in b]
        moved = [b[k][0] for k in b if k in a and a[k][1] != b[k][1]]
        if gained or lost or moved:
            changed.append(dict(brief(new_vis[node]), fields_added=sorted(gained), fields_removed=sorted(lost),
                                roles_changed=sorted(moved)))
    order = lambda v: (v["page_order"], v["page"], v["title"])
    return {
        "added": [brief(v) for v in sorted((new_vis[n] for n in new_vis if n not in old_vis), key=order)],
        "removed": [brief(v) for v in sorted((old_vis[n] for n in old_vis if n not in new_vis), key=order)],
        "changed": changed,
    }


def _visuals(n: int) -> str:
    return f"{n} visual" + ("" if n == 1 else "s")


def as_text(review: dict) -> str:
    """The review as plain text: for the terminal, an e-mail or a Teams message."""
    v, a = review["version"], review["against"]
    head = f"{v['model_name']} v{v['number']} (saved {v['file_modified_at'][:16].replace('T', ' ')})"
    if review["baseline"]:
        return f"{head}: first version, nothing to compare with"
    if v["status"] == "error":
        return f"{head}: could not be read ({v['note']})"
    lines = [f"{head}, compared with v{a['number']}"]
    c = review["counts"]
    if not c["total"]:
        lines.append("  no changes to measures, columns or visuals")
    for item in review["added"]:
        lines.append(f"  + {item['kind']} {item['label']}")
    for item in review["removed"]:
        lines.append(f"  - {item['kind']} {item['label']}" + (f"  (was in {_visuals(item['impact'])})" if item["impact"] else ""))
    for item in review["renamed"]:
        lines.append(f"  ~ renamed {item['old_label']} -> {item['label']}" + ("  (probably)" if item["guessed"] else ""))
    follow = [i["label"] for i in review["edited"] if i.get("follows_rename") and i["changes"] == ["DAX"]]
    if follow:
        lines.append(f"  ~ {len(follow)} expressions only follow the rename: {', '.join(follow)}")
    for item in review["edited"]:
        if item.get("follows_rename") and item["changes"] == ["DAX"]:
            continue
        what = ", ".join(item["changes"])
        if item.get("dax", {}).get("formatting_only"):
            what = what.replace("DAX", "DAX formatting")
        lines.append(f"  * {item['label']}: {what}" + (f"  (feeds {_visuals(item['impact'])})" if item["impact"] else ""))
        for line in (item.get("dax") or {}).get("lines", []):
            if line["op"] != " ":
                lines.append(f"      {line['op']} {line['text']}")
    for item in review["usage"]:
        lines.append(f"  ! {item['label']} {'is now unused' if item['became_unused'] else 'is used again'}")
    for item in review["broken"]:
        lines.append(f"  ! broken: {item['label']} is gone but still used by {', '.join(item['used_by'])}")
    for item in review["fixed"]:
        lines.append(f"  ok fixed: {item['label']} no longer referenced while missing")
    vis = review["visuals"]
    for item in vis["added"]:
        lines.append(f"  + visual {item['title']} on {item['page']}")
    for item in vis["removed"]:
        lines.append(f"  - visual {item['title']} on {item['page']}")
    for item in vis["changed"]:
        parts = ([f"+{f}" for f in item["fields_added"]] + [f"-{f}" for f in item["fields_removed"]]
                 + [f"~{f}" for f in item["roles_changed"]])
        lines.append(f"  * visual {item['title']} on {item['page']}: {', '.join(parts)}")
    return "\n".join(lines)
