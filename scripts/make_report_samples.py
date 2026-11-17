"""Sample Power BI reports for the sample models, in every shape Power BI writes.

    python3 scripts/make_report_samples.py                 # docs/samples/sample_report*.json
    python3 scripts/make_report_samples.py --fixtures DIR  # + .pbix, .pbit, PBIR zip for tests

One report spec, rendered four ways, so the tests can check that every
format gives the same answer:

  *_layout.json   the legacy report layout (what a .pbix keeps in Report/Layout)
  *.pbix          a zip with Report/Layout in UTF-16LE, as Power BI writes it
                  (report only: not a file Power BI Desktop can open)
  *.pbit          Report/Layout + DataModelSchema: report and model together
  *_pbir.zip      a PBIP report folder in the PBIR format, one visual.json each
"""

from __future__ import annotations

import csv
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# field: (kind, table, name[, level])  kind: measure | column | agg | datehier
M = lambda name, table="Measures": ("measure", table, name)
C = lambda table, name: ("column", table, name)
AGG = lambda table, name: ("agg", table, name)
HIER = lambda table, name, level: ("datehier", table, name, level)

SALES_REPORT = {
    "name": "Sales report",
    "filters": [C("Date", "IsCurrentYear")],
    "pages": [
        {"id": "overview", "display": "Overview", "filters": [C("Date", "Year")], "visuals": [
            {"id": "card1", "type": "card", "title": "Net sales", "roles": {"Values": [M("Total Sales")]}},
            {"id": "card2", "type": "card", "title": "Gross margin %", "roles": {"Values": [M("Gross Margin %")]}},
            {"id": "card3", "type": "card", "title": "Growth on last year", "roles": {"Values": [M("Sales YoY %")]}},
            {"id": "line1", "type": "lineChart", "title": "Sales by month",
             "roles": {"Category": [C("Date", "Month")], "Y": [M("Total Sales"), M("Sales PY")]}},
            {"id": "line2", "type": "lineChart", "title": "Orders over time",
             "roles": {"Category": [HIER("Sales", "OrderDate", "Year")], "Y": [M("Order Count")]}},
        ]},
        {"id": "customers", "display": "Customers", "filters": [], "visuals": [
            {"id": "table1", "type": "tableEx", "title": "Customer detail",
             "roles": {"Values": [C("Customer", "CustomerName"), C("Customer", "Country"),
                                  M("Sales per Customer", "Customer"), M("Order Count")]},
             "visual_filters": [C("Customer", "Segment")]},
            {"id": "bar1", "type": "clusteredBarChart", "title": "Share by segment",
             "roles": {"Category": [C("Customer", "Segment")], "Y": [M("Segment Share of Sales", "Customer")]},
             "formatting": [M("Gross Margin %")]},
            {"id": "slicer1", "type": "slicer", "title": "", "roles": {"Values": [C("Customer", "Country")]}},
        ]},
        {"id": "products", "display": "Products", "filters": [], "visuals": [
            {"id": "bar2", "type": "clusteredBarChart", "title": "Top products",
             "roles": {"Category": [C("Product", "ProductName")], "Y": [M("Total Sales")]},
             "topn_by": M("Total Sales")},
            {"id": "matrix1", "type": "pivotTable", "title": "Margin by category",
             "roles": {"Rows": [C("Product", "Category")],
                       "Values": [M("Gross Margin"), M("Avg Category Margin %"), AGG("Sales", "Quantity")]}},
        ]},
    ],
}

PROBLEMS_REPORT = {
    "name": "Report with problems",
    "filters": [],
    "pages": [
        {"id": "summary", "display": "Summary", "filters": [], "visuals": [
            {"id": "card1", "type": "card", "title": "Revenue", "roles": {"Values": [M("Revenue", "Sales")]}},
            {"id": "card2", "type": "card", "title": "Net revenue", "roles": {"Values": [M("Revenue Net", "Sales")]}},
            # a field the model no longer has: the visual shows an error in Power BI
            {"id": "card3", "type": "card", "title": "Against target", "roles": {"Values": [M("Sales Target", "Sales")]}},
        ]},
    ],
}


# ---------------------------------------------------------------------------
# legacy layout

def _ref(field, source=None, entity=None):
    sref = {"Source": source} if source else {"Entity": entity or field[1]}
    kind = field[0]
    if kind == "measure":
        return {"Measure": {"Expression": {"SourceRef": sref}, "Property": field[2]}}
    if kind == "column":
        return {"Column": {"Expression": {"SourceRef": sref}, "Property": field[2]}}
    if kind == "agg":
        return {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": sref}, "Property": field[2]}},
                                "Function": 0}}
    if kind == "datehier":
        return {"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {"PropertyVariationSource": {
            "Expression": {"SourceRef": sref}, "Name": "Variation", "Property": field[2]}},
            "Hierarchy": "Date Hierarchy"}}, "Level": field[3]}}
    raise ValueError(kind)


def _query_ref(field):
    if field[0] == "agg":
        return f"Sum({field[1]}.{field[2]})"
    if field[0] == "datehier":
        return f"{field[1]}.{field[2]}.Variation.Date Hierarchy.{field[3]}"
    return f"{field[1]}.{field[2]}"


def _filter(field, index):
    alias = field[1][0].lower()
    return {
        "name": f"Filter{index}",
        "expression": _ref(field),
        "filter": {"Version": 2, "From": [{"Name": alias, "Entity": field[1], "Type": 0}],
                   "Where": [{"Condition": {"Not": {"Expression": {"In": {
                       "Expressions": [_ref(field, source=alias)],
                       "Values": [[{"Literal": {"Value": "null"}}]]}}}}}]},
        "type": "Categorical",
        "howCreated": 1,
    }


def _topn_filter(column, measure):
    return {
        "name": "FilterTopN",
        "expression": _ref(column),
        "filter": {"Version": 2, "From": [{"Name": "subquery", "Expression": {"Subquery": {"Query": {
            "Version": 2,
            "From": [{"Name": "p", "Entity": column[1], "Type": 0}, {"Name": "m", "Entity": measure[1], "Type": 0}],
            "Select": [{**_ref(column, source="p"), "Name": "field"}],
            "OrderBy": [{"Direction": 2, "Expression": _ref(measure, source="m")}],
            "Top": 5}}}, "Type": 2}, {"Name": "p", "Entity": column[1], "Type": 0}],
            "Where": [{"Condition": {"In": {"Expressions": [_ref(column, source="p")],
                                            "Table": {"SourceRef": {"Source": "subquery"}}}}}]},
        "type": "TopN",
    }


def _title(text):
    return [{"properties": {"text": {"expr": {"Literal": {"Value": "'" + text.replace("'", "''") + "'"}}}}}]


def _fill_rule(field):
    return {"dataPoint": [{"properties": {"fill": {"solid": {"color": {"expr": {"FillRule": {
        "Input": _ref(field), "FillRule": {"linearGradient2": {
            "min": {"color": {"Literal": {"Value": "'#cde2fb'"}}},
            "max": {"color": {"Literal": {"Value": "'#184f95'"}}}}}}}}}}}}]}


def layout(spec):
    sections = []
    for order, page in enumerate(spec["pages"]):
        containers = []
        for v in page["visuals"]:
            fields = [f for fs in v["roles"].values() for f in fs]
            aliases, froms = {}, []
            for f in fields:
                if f[1] not in aliases:
                    aliases[f[1]] = f"{f[1][0].lower()}{len(aliases)}"
                    froms.append({"Name": aliases[f[1]], "Entity": f[1], "Type": 0})
            select, projections = [], {}
            for role, fs in v["roles"].items():
                for f in fs:
                    qref = _query_ref(f)
                    if not any(s["Name"] == qref for s in select):
                        select.append({**_ref(f, source=aliases[f[1]]), "Name": qref})
                    projections.setdefault(role, []).append({"queryRef": qref})
            single = {"visualType": v["type"], "projections": projections,
                      "prototypeQuery": {"Version": 2, "From": froms, "Select": select}}
            if v.get("title"):
                single["vcObjects"] = {"title": _title(v["title"])}
            if v.get("formatting"):
                single["objects"] = _fill_rule(v["formatting"][0])
            filters = [_filter(f, i) for i, f in enumerate(v.get("visual_filters", []))]
            if v.get("topn_by"):
                filters.append(_topn_filter(v["roles"]["Category"][0], v["topn_by"]))
            config = {"name": v["id"], "layouts": [{"id": 0, "position": {"x": 0, "y": 0, "width": 300, "height": 200}}],
                      "singleVisual": single}
            containers.append({"x": 0, "y": 0, "z": 0, "width": 300, "height": 200,
                               "config": json.dumps(config), "filters": json.dumps(filters)})
        sections.append({"id": order, "name": f"ReportSection{page['id']}", "displayName": page["display"],
                         "ordinal": order, "filters": json.dumps([_filter(f, i) for i, f in enumerate(page["filters"])]),
                         "visualContainers": containers, "config": "{}", "width": 1280, "height": 720})
    return {"id": 0, "sections": sections, "config": json.dumps({"version": "5.55", "themeCollection": {}}),
            "filters": json.dumps([_filter(f, i) for i, f in enumerate(spec["filters"])]),
            "layoutOptimization": 0, "resourcePackages": []}


# ---------------------------------------------------------------------------
# PBIR

def pbir_files(spec):
    base = f"{spec['name']}.Report/definition"
    files = {
        f"{base}/report.json": {"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/1.0.0/schema.json",
                                "filterConfig": {"filters": [{"name": f"F{i}", "field": _ref(f), "type": "Categorical"}
                                                             for i, f in enumerate(spec["filters"])]}},
        f"{base}/pages/pages.json": {"pageOrder": [p["id"] for p in spec["pages"]], "activePageName": spec["pages"][0]["id"]},
    }
    for page in spec["pages"]:
        files[f"{base}/pages/{page['id']}/page.json"] = {
            "name": page["id"], "displayName": page["display"], "displayOption": "FitToPage",
            "filterConfig": {"filters": [{"name": f"P{i}", "field": _ref(f), "type": "Categorical"}
                                         for i, f in enumerate(page["filters"])]}}
        for v in page["visuals"]:
            query_state = {role: {"projections": [{"field": _ref(f), "queryRef": _query_ref(f), "nativeQueryRef": f[2]}
                                                  for f in fs]} for role, fs in v["roles"].items()}
            visual = {"visualType": v["type"], "query": {"queryState": query_state}}
            if v.get("title"):
                visual["visualContainerObjects"] = {"title": _title(v["title"])}
            if v.get("formatting"):
                visual["objects"] = _fill_rule(v["formatting"][0])
            filters = [{"name": f"V{i}", "field": _ref(f), "type": "Categorical"}
                       for i, f in enumerate(v.get("visual_filters", []))]
            if v.get("topn_by"):
                topn = _topn_filter(v["roles"]["Category"][0], v["topn_by"])
                filters.append({"name": "VTopN", "field": topn["expression"], "type": "TopN", "filter": topn["filter"]})
            files[f"{base}/pages/{page['id']}/visuals/{v['id']}/visual.json"] = {
                "name": v["id"], "position": {"x": 0, "y": 0, "z": 0, "width": 300, "height": 200},
                "visual": visual, "filterConfig": {"filters": filters}}
    return files


# ---------------------------------------------------------------------------
# DataModelSchema (.pbit) from the CSV exports

def data_model_schema(folder):
    read = lambda name: list(csv.DictReader((folder / name).open(encoding="utf-8", newline="")))
    tables = {r["ID"]: {"name": r["Name"], "columns": [], "measures": []} for r in read("info_tables.csv")}
    col_names = {}
    columns_path = folder / "info_columns.csv"
    for c in (read("info_columns.csv") if columns_path.exists() else []):
        col_names[c["ID"]] = (tables[c["TableID"]]["name"], c["ExplicitName"])
    for c in (read("info_columns.csv") if columns_path.exists() else []):
        column = {"name": c["ExplicitName"], "dataType": (c.get("DataType") or "string").lower(),
                  "isHidden": c.get("IsHidden") == "True", "lineageTag": c.get("LineageTag", "")}
        if c.get("Expression"):
            column.update({"type": "calculated", "expression": c["Expression"]})
        else:
            column["sourceColumn"] = c["ExplicitName"]
        if c.get("SortByColumnID"):
            column["sortByColumn"] = col_names[c["SortByColumnID"]][1]
        if c.get("DisplayFolder"):
            column["displayFolder"] = c["DisplayFolder"]
        tables[c["TableID"]]["columns"].append(column)
    for m in read("info_measures.csv"):
        expression = m["Expression"]
        measure = {"name": m["Name"], "expression": expression.split("\n") if "\n" in expression else expression}
        for key, src in (("formatString", "FormatString"), ("displayFolder", "DisplayFolder"), ("description", "Description")):
            if m.get(src):
                measure[key] = m[src]
        if m.get("IsHidden") == "True":
            measure["isHidden"] = True
        tables[m["TableID"]]["measures"].append(measure)
    relationships = []
    rel_path = folder / "info_relationships.csv"
    for r in (read("info_relationships.csv") if rel_path.exists() else []):
        (ft, fc), (tt, tc) = col_names[r["FromColumnID"]], col_names[r["ToColumnID"]]
        relationships.append({"name": f"r{r['ID']}", "fromTable": ft, "fromColumn": fc, "toTable": tt, "toColumn": tc})
    return {"name": "Model", "compatibilityLevel": 1567,
            "model": {"culture": "en-US", "tables": list(tables.values()), "relationships": relationships}}


def utf16(obj):
    return json.dumps(obj).encode("utf-16-le")


def write_fixtures(target: Path):
    target.mkdir(parents=True, exist_ok=True)
    for spec, folder, stem in ((SALES_REPORT, ROOT / "examples", "sales"),
                               (PROBLEMS_REPORT, ROOT / "examples" / "troubled", "problems")):
        with zipfile.ZipFile(target / f"{stem}.pbix", "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("Version", "1.28".encode("utf-16-le"))
            z.writestr("Report/Layout", utf16(layout(spec)))
            z.writestr("DataModel", b"\x00" * 4096)          # stands in for the VertiPaq blob
        with zipfile.ZipFile(target / f"{stem}.pbit", "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("Report/Layout", utf16(layout(spec)))
            z.writestr("DataModelSchema", utf16(data_model_schema(folder)))
        with zipfile.ZipFile(target / f"{stem}_pbir.zip", "w", zipfile.ZIP_DEFLATED) as z:
            for path, body in pbir_files(spec).items():
                z.writestr(path, json.dumps(body, indent=2))
        (target / f"{stem}_layout.json").write_text(json.dumps(layout(spec), indent=1), encoding="utf-8")
    return sorted(p.name for p in target.iterdir())


def write_samples():
    out = ROOT / "docs" / "samples"
    for spec, name in ((SALES_REPORT, "sample_report.json"),
                       (PROBLEMS_REPORT, "sample_report_with_problems.json")):
        (out / name).write_text(json.dumps(layout(spec), indent=1), encoding="utf-8")
        print(f"wrote docs/samples/{name}")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--fixtures":
        print("\n".join(write_fixtures(Path(sys.argv[2]))))
    else:
        write_samples()
