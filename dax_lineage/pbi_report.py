"""Which model fields a Power BI report uses, read from the report file itself.

The same rules as docs/js/report.js, so the CLI and the web page agree:

    .pbix   Report/Layout (legacy JSON, UTF-16LE). Its model is binary: pair it
            with the INFO.* export.
    .pbit   Report/Layout + DataModelSchema (the model as JSON).
    PBIP    <name>.Report/definition/pages/<p>/visuals/<v>/visual.json (PBIR),
            or <name>.Report/report.json (PBIR-legacy), and
            <name>.SemanticModel/model.bim when the model is saved as TMSL.

Each usage is a dict: report, page, pageOrder, visualId, visualType, title,
role, kind ("measure" | "column"), table, name.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ROLE_NAMES = {
    "Category": "Axis", "Y": "Values", "Y2": "Secondary values", "X": "X axis", "Series": "Legend",
    "Values": "Values", "Rows": "Rows", "Columns": "Columns", "Tooltips": "Tooltips", "Details": "Details",
    "Size": "Size", "Indicator": "Value", "Goal": "Target", "TrendLine": "Trend axis", "Data": "Values",
    "Fields": "Fields", "Play": "Play axis", "Gradient": "Color saturation", "MinValue": "Minimum",
    "MaxValue": "Maximum", "TargetValue": "Target",
}


def role_name(role: str) -> str:
    return ROLE_NAMES.get(role, role)


def decode(data: bytes) -> str:
    """Power BI writes Report/Layout and DataModelSchema as UTF-16LE, usually without a BOM."""
    if data[:2] == b"\xff\xfe":
        return data[2:].decode("utf-16-le")
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be")
    if len(data) > 3 and data[1] == 0 and data[3] == 0 and data[0] != 0:
        return data.decode("utf-16-le")
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8")
    return data.decode("utf-8")


def _parse_maybe(value):
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not (text.startswith("{") or text.startswith("[")):
        return value
    try:
        return json.loads(text)
    except ValueError:
        return value


def _literal(text_property) -> str:
    try:
        value = text_property["expr"]["Literal"]["Value"]
    except (KeyError, TypeError):
        return ""
    if not isinstance(value, str):
        return ""
    match = re.fullmatch(r"'([\s\S]*)'", value)
    return (match.group(1) if match else value).replace("''", "'")


def _dig(obj, *keys):
    for key in keys:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int) and len(obj) > key:
            obj = obj[key]
        else:
            return None
    return obj


# -- field references -------------------------------------------------------

def _entity(expression, aliases) -> str:
    ref = _dig(expression, "SourceRef")
    if not isinstance(ref, dict):
        return ""
    return ref.get("Entity") or aliases.get(ref.get("Source"), "") or ""


def _field_ref(node: dict, aliases: dict):
    measure, column = node.get("Measure"), node.get("Column")
    if isinstance(measure, dict) and isinstance(measure.get("Property"), str):
        return {"kind": "measure", "table": _entity(measure.get("Expression"), aliases), "name": measure["Property"]}
    if isinstance(column, dict) and isinstance(column.get("Property"), str):
        return {"kind": "column", "table": _entity(column.get("Expression"), aliases), "name": column["Property"]}
    level = node.get("HierarchyLevel")
    if isinstance(level, dict):
        hierarchy = _dig(level, "Expression", "Hierarchy")
        if not isinstance(hierarchy, dict):
            return None
        # auto date/time: the level hangs off a variation of a real date column
        variation = _dig(hierarchy, "Expression", "PropertyVariationSource")
        if isinstance(variation, dict) and variation.get("Property"):
            return {"kind": "column", "table": _entity(variation.get("Expression"), aliases),
                    "name": variation["Property"]}
        if isinstance(level.get("Level"), str):
            return {"kind": "column", "table": _entity(hierarchy.get("Expression"), aliases), "name": level["Level"]}
    return None


def collect_refs(value, aliases: Optional[dict] = None, out: Optional[list] = None) -> list:
    """Every field reference inside a piece of report JSON."""
    aliases = aliases or {}
    out = [] if out is None else out
    value = _parse_maybe(value)
    if isinstance(value, list):
        for item in value:
            collect_refs(item, aliases, out)
        return out
    if not isinstance(value, dict):
        return out
    scope = aliases
    if isinstance(value.get("From"), list):
        scope = dict(aliases)
        for source in value["From"]:
            if isinstance(source, dict) and source.get("Name"):
                scope[source["Name"]] = source.get("Entity") or scope.get(source["Name"])
    ref = _field_ref(value, scope)
    if ref:
        out.append(ref)
        return out
    for key, item in value.items():
        # a selector pins formatting to one data point; it names a field but
        # does not use it, and Power BI keeps stale ones after a rename
        if key == "selector":
            continue
        collect_refs(item, scope, out)
    return out


def _dedupe(usages: list) -> list:
    seen, out = set(), []
    for u in usages:
        if not u.get("name"):
            continue
        key = tuple(str(u[k]).lower() for k in ("report", "visualId", "role", "kind", "table", "name"))
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def _push(usages: list, base: dict, refs: list, role: str) -> None:
    for ref in refs:
        usages.append({**base, "role": role, **ref})


# -- legacy layout ----------------------------------------------------------

def parse_layout(layout, report_name: str) -> list:
    layout = _parse_maybe(layout)
    usages: list = []
    _push(usages, {"report": report_name, "page": "(all pages)", "pageOrder": -1, "visualId": "report-filters",
                   "visualType": "report filters", "title": "Report filters"},
          collect_refs(layout.get("filters")), "Report filter")
    for index, section in enumerate(layout.get("sections") or []):
        page = section.get("displayName") or section.get("name") or f"Page {index + 1}"
        order = section.get("ordinal") if isinstance(section.get("ordinal"), int) else index
        section_id = section.get("name") or index
        _push(usages, {"report": report_name, "page": page, "pageOrder": order,
                       "visualId": f"{section_id}:page-filters", "visualType": "page filters", "title": "Page filters"},
              collect_refs(section.get("filters")), "Page filter")
        for container in section.get("visualContainers") or []:
            config = _parse_maybe(container.get("config")) or {}
            visual = config.get("singleVisual") if isinstance(config, dict) else None
            base = {
                "report": report_name, "page": page, "pageOrder": order,
                "visualId": f"{section_id}:{config.get('name') or len(usages)}",
                "visualType": (visual.get("visualType") or "visual") if visual else "group",
                "title": _literal(_dig(visual, "vcObjects", "title", 0, "properties", "text")) if visual else "",
            }
            if visual:
                roles: dict = {}
                for role, items in (visual.get("projections") or {}).items():
                    for item in items or []:
                        roles.setdefault(item.get("queryRef"), []).append(role_name(role))
                query = visual.get("prototypeQuery") or {}
                aliases = {f["Name"]: f.get("Entity") for f in query.get("From") or [] if isinstance(f, dict) and f.get("Name")}
                for select in query.get("Select") or []:
                    refs = collect_refs(select, aliases)
                    for role in roles.get(select.get("Name"), ["Query"]):
                        _push(usages, base, refs, role)
                _push(usages, base, collect_refs({"Where": query.get("Where"), "OrderBy": query.get("OrderBy")}, aliases),
                      "Sort or query filter")
                _push(usages, base, collect_refs(visual.get("objects"), aliases), "Conditional formatting")
                _push(usages, base, collect_refs(visual.get("vcObjects"), aliases), "Formatting")
            _push(usages, base, collect_refs(container.get("filters")), "Visual filter")
    return _dedupe(usages)


# -- PBIR -------------------------------------------------------------------

PBIR_VISUAL = re.compile(r"(^|/)definition/pages/([^/]+)/visuals/([^/]+)/visual\.json$", re.I)
PBIR_PAGE = re.compile(r"(^|/)definition/pages/([^/]+)/page\.json$", re.I)
PBIR_PAGES = re.compile(r"(^|/)definition/pages/pages\.json$", re.I)
PBIR_REPORT = re.compile(r"(^|/)definition/report\.json$", re.I)


def parse_pbir(paths: list, get_text: Callable[[str], str], report_name: str) -> list:
    def load(p):
        return _parse_maybe(get_text(p)) or {}

    usages: list = []
    pages_file = next((p for p in paths if PBIR_PAGES.search(p)), None)
    order = (load(pages_file).get("pageOrder") or []) if pages_file else []
    position = lambda pid: order.index(pid) if pid in order else -1
    pages = {}
    for p in [p for p in paths if PBIR_PAGE.search(p)]:
        page_id = PBIR_PAGE.search(p).group(2)
        page = load(p)
        pages[page_id] = {"name": page.get("displayName") or page.get("name") or page_id, "order": position(page_id)}
        _push(usages, {"report": report_name, "page": pages[page_id]["name"], "pageOrder": pages[page_id]["order"],
                       "visualId": f"{page_id}:page-filters", "visualType": "page filters", "title": "Page filters"},
              collect_refs(page.get("filterConfig")), "Page filter")
    report_file = next((p for p in paths if PBIR_REPORT.search(p)), None)
    if report_file:
        _push(usages, {"report": report_name, "page": "(all pages)", "pageOrder": -1, "visualId": "report-filters",
                       "visualType": "report filters", "title": "Report filters"},
              collect_refs(load(report_file).get("filterConfig")), "Report filter")
    for p in [p for p in paths if PBIR_VISUAL.search(p)]:
        match = PBIR_VISUAL.search(p)
        page_id, visual_id = match.group(2), match.group(3)
        container = load(p)
        visual = container.get("visual")
        page = pages.get(page_id, {"name": page_id, "order": position(page_id)})
        title = _literal(_dig(visual, "visualContainerObjects", "title", 0, "properties", "text")) if visual else ""
        base = {"report": report_name, "page": page["name"], "pageOrder": page["order"],
                "visualId": f"{page_id}:{container.get('name') or visual_id}",
                "visualType": (visual.get("visualType") or "visual") if visual else "group", "title": title or ""}
        if visual:
            for role, bucket in (_dig(visual, "query", "queryState") or {}).items():
                for projection in (bucket or {}).get("projections") or []:
                    _push(usages, base, collect_refs(projection.get("field")), role_name(role))
            _push(usages, base, collect_refs(_dig(visual, "query", "sortDefinition")), "Sort or query filter")
            _push(usages, base, collect_refs(visual.get("objects")), "Conditional formatting")
            _push(usages, base, collect_refs(visual.get("visualContainerObjects")), "Formatting")
        _push(usages, base, collect_refs(container.get("filterConfig")), "Visual filter")
    return _dedupe(usages)


# -- the model as JSON ------------------------------------------------------

def _lines(value) -> str:
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    return "" if value is None else str(value)


def model_rows(schema) -> Optional[dict]:
    """DataModelSchema / model.bim as rows shaped like the INFO.* exports."""
    schema = _parse_maybe(schema)
    model = schema.get("model", schema) if isinstance(schema, dict) else None
    if not isinstance(model, dict) or not isinstance(model.get("tables"), list):
        return None
    tables, columns, measures, relationships = [], [], [], []
    for index, table in enumerate(model["tables"], start=1):
        tables.append({"ID": index, "Name": table.get("name"), "Description": _lines(table.get("description")),
                       "IsHidden": bool(table.get("isHidden"))})
        for column in table.get("columns") or []:
            if column.get("type") == "rowNumber":
                continue
            columns.append({"TableID": index, "ExplicitName": column.get("name"),
                            "Expression": _lines(column.get("expression")) if column.get("type") == "calculated" else "",
                            "IsHidden": bool(column.get("isHidden")), "DisplayFolder": column.get("displayFolder") or "",
                            "DataType": column.get("dataType") or "", "SortByColumn": column.get("sortByColumn") or ""})
        for measure in table.get("measures") or []:
            measures.append({"TableID": index, "Name": measure.get("name"), "Expression": _lines(measure.get("expression")),
                             "DisplayFolder": measure.get("displayFolder") or "", "IsHidden": bool(measure.get("isHidden")),
                             "Description": _lines(measure.get("description")), "FormatString": measure.get("formatString") or "",
                             "State": "Ready"})
    for rel in model.get("relationships") or []:
        relationships.append({"FromTable": rel.get("fromTable"), "FromColumn": rel.get("fromColumn"),
                              "ToTable": rel.get("toTable"), "ToColumn": rel.get("toColumn"),
                              "IsActive": rel.get("isActive", True) is not False})
    return {"measures": measures, "tables": tables, "columns": columns, "relationships": relationships}


# -- entry points -----------------------------------------------------------

@dataclass
class ReportResult:
    usages: list = field(default_factory=list)
    model: Optional[dict] = None
    found: list = field(default_factory=list)
    note: str = ""


def _is_layout(obj) -> bool:
    return isinstance(obj, dict) and isinstance(obj.get("sections"), list)


def _is_model(obj) -> bool:
    return isinstance(obj, dict) and (isinstance(obj.get("tables"), list) or isinstance(_dig(obj, "model", "tables"), list))


def _stem(name: str) -> str:
    return re.sub(r"\.(pbix|pbit|zip|json)$", "", re.split(r"[\\/]", name.rstrip("/\\"))[-1], flags=re.I)


def _from_paths(paths: list, get_text: Callable[[str], str], label: str) -> ReportResult:
    result = ReportResult()

    def report_name(p):
        match = re.search(r"([^/]+)\.Report/", p, re.I)
        return match.group(1) if match else label

    pbir = [p for p in paths if PBIR_VISUAL.search(p) or PBIR_PAGE.search(p) or PBIR_PAGES.search(p) or PBIR_REPORT.search(p)]
    groups: dict = {}
    for p in pbir:
        groups.setdefault(report_name(p), []).append(p)
    for name, group in groups.items():
        result.usages.extend(parse_pbir(group, get_text, name))
        result.found.append(f"{name} (PBIR report)")
    for p in paths:
        base = p.rsplit("/", 1)[-1].lower()
        if re.search(r"(^|/)report/layout$", p, re.I) or (base == "report.json" and not PBIR_REPORT.search(p)):
            layout = _parse_maybe(get_text(p))
            if _is_layout(layout):
                name = report_name(p) if re.search(r"\.report/", p, re.I) else label
                result.usages.extend(parse_layout(layout, name))
                result.found.append(f"{name} (report layout)")
        elif base in ("datamodelschema", "model.bim") and result.model is None:
            schema = _parse_maybe(get_text(p))
            rows = model_rows(schema) if _is_model(schema) else None
            if rows:
                result.model = rows
                result.found.append(f"{label} (model from {'model.bim' if base == 'model.bim' else 'DataModelSchema'})")
    return result


def read_report(path) -> Optional[ReportResult]:
    """Read a .pbix, .pbit, PBIP folder or its zip, report layout or model.bim."""
    path = Path(path)
    lower = path.name.lower()
    if path.is_dir():
        files = {}
        for folder, _dirs, names in os.walk(path):
            for name in names:
                full = Path(folder) / name
                files[full.relative_to(path.parent).as_posix()] = full
        return _from_paths(sorted(files), lambda p: decode(files[p].read_bytes()), _stem(path.name))
    if lower.endswith((".pbix", ".pbit", ".zip")):
        with zipfile.ZipFile(path) as archive:
            names = [n.replace("\\", "/") for n in archive.namelist()]
            lookup = {n.replace("\\", "/"): n for n in archive.namelist()}
            result = _from_paths(names, lambda p: decode(archive.read(lookup[p])), _stem(path.name))
            has_model = "DataModel" in lookup
            if lower.endswith(".pbix") and result.model is None:
                if not has_model:
                    result.note = (f"{path.name} has no model of its own: it is a thin report on a published "
                                   "semantic model. Add the INFO.MEASURES export from that model.")
                else:
                    from .pbix_model import read_model
                    try:
                        result.model = read_model(archive.read(lookup["DataModel"]))
                        result.found.append(f"{_stem(path.name)} (model from the .pbix: "
                                            f"{len(result.model['measures'])} measures, {len(result.model['columns'])} columns)")
                    except ValueError as err:
                        result.note = (f"{path.name}: its model could not be read ({err}). "
                                       "Add the INFO.MEASURES export, or save the file as .pbit.")
        return result
    if lower.endswith((".json", ".bim")) or lower == "layout":
        try:
            obj = json.loads(decode(path.read_bytes()))
        except ValueError:
            return None
        if _is_layout(obj):
            return ReportResult(usages=parse_layout(obj, _stem(path.name)), found=[f"{_stem(path.name)} (report layout)"])
        if _is_model(obj):
            return ReportResult(model=model_rows(obj), found=[f"{_stem(path.name)} (model)"])
    return None


def is_report_path(path) -> bool:
    path = Path(path)
    if path.is_dir():
        return True
    return path.suffix.lower() in (".pbix", ".pbit", ".zip", ".bim") or path.name.lower() == "layout"
