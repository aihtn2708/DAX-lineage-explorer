"""The team's modelling standards, as rules that run on every scanned version.

Each rule looks at one version of a model and says how many objects it applies
to and which of them fail. The score of a version is the weighted share that
pass: errors weigh 3, warnings 2, notes 1, so one missing description moves the
score less than one broken visual. Rules the team turns off do not count, and
an object the team has accepted as an exception counts as passing.

Every rule can be turned off or given another severity, and some take
parameters; the defaults are what most Power BI teams agree on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from ..dax import tokenize

SEVERITIES = ("error", "warning", "info")
WEIGHT = {"error": 3, "warning": 2, "info": 1}


@dataclass
class Rule:
    id: str
    title: str
    why: str
    severity: str
    check: Callable
    enabled: bool = True
    params: dict = field(default_factory=dict)
    needs_report: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "why": self.why, "severity": self.severity,
                "enabled": self.enabled, "params": dict(self.params), "needs_report": self.needs_report}


def label(o: dict) -> str:
    if o["kind"] in ("measure", "table") or not o.get("table_name"):
        return o["name"]
    return f"{o['table_name']}[{o['name']}]"


def _tokens(text: str) -> list:
    try:
        return [t for t in tokenize(text or "") if t.kind != "comment"]
    except Exception:
        return []


class Context:
    """One version, shaped for the rules."""

    def __init__(self, rows: dict, has_report: bool):
        self.has_report = has_report
        self.objects = [o for o in rows["objects"] if o["in_model"]]
        self.missing = [o for o in rows["objects"] if not o["in_model"]]
        self.measures = [o for o in self.objects if o["kind"] == "measure"]
        self.columns = [o for o in self.objects if o["kind"] == "column"]
        self.by_node = {o["node"]: o for o in rows["objects"]}
        self.readers: dict = {}
        for d in rows["dependencies"]:
            src = self.by_node.get(d["source"])
            self.readers.setdefault(d["target"], []).append(label(src) if src else d["source"])
        visuals = {v["node"]: v for v in rows["visuals"]}
        for u in rows["usages"]:
            v = visuals.get(u["visual"], {})
            self.readers.setdefault(u["field"], []).append(
                f"visual {v.get('title') or v.get('visual_type') or u['visual']} on {v.get('page', '?')}")


def visible(objects: list) -> list:
    return [o for o in objects if not o["is_hidden"]]


# -- the rules ------------------------------------------------------------------

def _description(ctx, params):
    items = visible(ctx.measures)
    return len(items), [(o, "no description") for o in items if not (o["description"] or "").strip()]


def _column_description(ctx, params):
    items = visible(ctx.columns)
    return len(items), [(o, "no description") for o in items if not (o["description"] or "").strip()]


def _format(ctx, params):
    items = visible(ctx.measures)
    return len(items), [(o, "no format string") for o in items if not (o["format_string"] or "").strip()]


def _folder(ctx, params):
    least = int(params.get("min_measures", 10))
    per_table: dict = {}
    for o in visible(ctx.measures):
        per_table.setdefault(o["table_name"], []).append(o)
    items = [o for group in per_table.values() if len(group) >= least for o in group]
    return len(items), [(o, f"not in a display folder ({len(per_table[o['table_name']])} measures in "
                            f"{o['table_name']})") for o in items if not (o["display_folder"] or "").strip()]


def _naming(ctx, params):
    pattern = (params.get("pattern") or "").strip()
    compiled = re.compile(pattern) if pattern else None
    found = []
    for o in ctx.measures:
        name, problems = o["name"], []
        if name != name.strip():
            problems.append("leading or trailing spaces")
        if "  " in name:
            problems.append("double spaces")
        if re.search(r"[\t\r\n]", name):
            problems.append("a tab or line break")
        message = "name has " + ", ".join(problems) if problems else ""
        if compiled and not compiled.search(name):
            message = (message + "; " if message else "") + f"name does not match {pattern}"
        if message:
            found.append((o, message))
    return len(ctx.measures), found


def _helper_hidden(ctx, params):
    prefix = params.get("prefix", "_") or "_"
    items = [o for o in ctx.measures if o["name"].startswith(prefix)]
    return len(items), [(o, f"starts with {prefix} but is visible") for o in items if not o["is_hidden"]]


def _broken(ctx, params):
    referenced = [o for o in ctx.objects + ctx.missing if o["node"] in ctx.readers]
    found = [(o, "used by " + ", ".join(sorted(set(ctx.readers[o["node"]]))[:6]) + ", but not in the model")
             for o in ctx.missing if o["node"] in ctx.readers]
    return len(referenced), found


def _state(ctx, params):
    return len(ctx.measures), [(o, f"{o['state']}" + (f": {o['error_message']}" if o.get("error_message") else ""))
                               for o in ctx.measures if o["state"] not in ("", "Ready")]


def _unused_measure(ctx, params):
    found = [(o, "nothing uses it" if o["usage_status"] == "unused" else "only unused measures use it")
             for o in ctx.measures if o["usage_status"] in ("unused", "deadChain")]
    return len(ctx.measures), found


def _unused_hidden_column(ctx, params):
    items = [o for o in ctx.columns if o["is_hidden"]]
    return len(items), [(o, "hidden and nothing uses it: it still takes memory") for o in items
                        if o["usage_status"] == "unused"]


def _divide(ctx, params):
    items = [o for o in ctx.measures if (o["expression"] or "").strip()]
    return len(items), [(o, "divides with /, which fails on zero; DIVIDE ( a, b ) returns blank instead")
                        for o in items if any(t.kind == "punct" and t.value == "/" for t in _tokens(o["expression"]))]


def _iferror(ctx, params):
    items = [o for o in ctx.measures if (o["expression"] or "").strip()]
    found = []
    for o in items:
        tokens = _tokens(o["expression"])
        names = [t.value.upper() for i, t in enumerate(tokens) if t.kind == "ident"
                 and t.value.casefold() in ("iferror", "iserror") and i + 1 < len(tokens) and tokens[i + 1].value == "("]
        if names:
            found.append((o, f"uses {', '.join(sorted(set(names)))}, which slows every evaluation"))
    return len(items), found


def _duplicate(ctx, params):
    groups: dict = {}
    for o in ctx.measures:
        tokens = _tokens(o["expression"])
        if len(tokens) > 1:
            key = tuple((t.kind, t.value if t.kind == "string" else t.value.casefold()) for t in tokens)
            groups.setdefault(key, []).append(o)
    found = []
    for group in groups.values():
        if len(group) > 1:
            for o in group:
                others = [g["name"] for g in group if g is not o]
                found.append((o, "same DAX as " + ", ".join(others)))
    return len(ctx.measures), found


def _depth(ctx, params):
    most = int(params.get("max_depth", 6))
    return len(ctx.measures), [(o, f"a chain {o['depth']} measures deep (more than {most})")
                               for o in ctx.measures if o.get("depth", 0) > most]


RULES = [
    Rule("broken-reference", "Nothing uses a field the model does not have",
         "A measure or visual that reads a removed or renamed field shows an error to every reader.",
         "error", _broken),
    Rule("measure-error", "Every measure is in the Ready state",
         "A measure in the Error state breaks every visual that shows it.", "error", _state),
    Rule("unused-measure", "Every measure is used",
         "Unused measures make the field list harder to read and are easy to use by mistake. Check reports on "
         "the published model before removing one.", "warning", _unused_measure, needs_report=True),
    Rule("measure-description", "Visible measures have a description",
         "The description is the tooltip report builders see in the field list.", "warning", _description),
    Rule("measure-format", "Visible measures have a format string",
         "Without one, every visual formats the number its own way.", "warning", _format),
    Rule("use-divide", "Divide with DIVIDE, not /",
         "/ returns an error when dividing by zero; DIVIDE returns blank, and is as fast.", "warning", _divide),
    Rule("measure-naming", "Measure names are tidy",
         "Stray spaces make two measures look the same. Add a regular expression to enforce a naming pattern.",
         "warning", _naming, params={"pattern": ""}),
    Rule("measure-folder", "Measures in busy tables sit in display folders",
         "Folders keep a long field list usable.", "info", _folder, params={"min_measures": 10}),
    Rule("helper-hidden", "Helper measures are hidden",
         "Measures named as helpers are building blocks, not for report builders.", "info", _helper_hidden,
         params={"prefix": "_"}),
    Rule("unused-hidden-column", "Hidden columns are used by something",
         "A hidden column nothing reads still takes memory and refresh time.", "info", _unused_hidden_column,
         needs_report=True),
    Rule("avoid-iferror", "Avoid IFERROR and ISERROR",
         "They force the engine to evaluate row by row; test for the case instead.", "info", _iferror),
    Rule("duplicate-dax", "No two measures have the same DAX",
         "Two copies drift apart: keep one and point the other at it, or remove it.", "info", _duplicate),
    Rule("column-description", "Visible columns have a description",
         "Off by default: worth it for shared semantic models.", "info", _column_description, enabled=False),
    Rule("measure-depth", "Measure chains stay shallow",
         "Off by default: very deep chains are hard to follow and to change safely.", "info", _depth,
         enabled=False, params={"max_depth": 6}),
]
BY_ID = {r.id: r for r in RULES}


def run(rules: list, rows: dict, has_report: bool) -> list:
    """[(rule dict, applicable, [(object, message)])] for each enabled rule."""
    ctx = Context(rows, has_report)
    out = []
    for rule in rules:
        if not rule["enabled"] or (BY_ID[rule["id"]].needs_report and not has_report):
            continue
        applicable, found = BY_ID[rule["id"]].check(ctx, rule["params"])
        out.append((rule, applicable, found))
    return out


def score(results: list, accepted: set) -> dict:
    """Weighted share of passing checks, 0-100, with accepted exceptions counted as passing."""
    total = earned = 0.0
    counts = {"error": 0, "warning": 0, "info": 0, "accepted": 0}
    per_rule = {}
    for rule, applicable, found in results:
        failing = [f for f in found if (rule["id"], f[0]["object_key"]) not in accepted]
        counts["accepted"] += len(found) - len(failing)
        counts[rule["severity"]] += len(failing)
        per_rule[rule["id"]] = {"applicable": applicable, "failed": len(failing), "accepted": len(found) - len(failing)}
        if applicable:
            weight = WEIGHT[rule["severity"]]
            total += weight
            earned += weight * max(0.0, 1 - len(failing) / applicable)
    return {"score": round(100 * earned / total) if total else 100, **counts, "rules": per_rule}
