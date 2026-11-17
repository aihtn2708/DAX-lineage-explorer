"""Apply an approved change to a model written as TMSL JSON (model.bim, or a .pbit's DataModelSchema).

The same rules as the Desktop companion (companion/PowerBIWorkspaceCompanion.ps1),
which does this to the model open in Power BI Desktop through the Tabular
Object Model. Keeping one reference here lets the tests run every change from
request to confirmation without Windows, and the companion is checked against
the same expectations.

For each edit:
- desktop      left for the author to do in Desktop          -> manual
- the object cannot be found                                 -> skipped (removing it -> already)
- the property already has the new value                     -> already
- the property no longer has the value the request expects   -> skipped
- otherwise the edit is made                                 -> applied
"""

from __future__ import annotations

PROPERTY = {"Name": "name", "Expression": "expression", "Description": "description",
            "DisplayFolder": "displayFolder", "FormatString": "formatString", "IsHidden": "isHidden"}


def _text(value) -> str:
    if isinstance(value, list):
        value = "\n".join(str(v) for v in value)
    return "" if value is None else str(value).replace("\r\n", "\n")


def _find(model: dict, op: dict):
    """(table, collection, item) for the measure or column an edit is about."""
    collection = "measures" if op["kind"] == "measure" else "columns"
    tables = model.get("tables") or []
    if op.get("lineage_tag"):
        for table in tables:
            for item in table.get(collection) or []:
                if item.get("lineageTag") == op["lineage_tag"]:
                    return table, collection, item
    for table in tables:
        if table.get("name", "").casefold() != (op.get("table") or "").casefold():
            continue
        for item in table.get(collection) or []:
            if item.get("name", "").casefold() == op["name"].casefold():
                return table, collection, item
    return None, None, None


def current(item: dict, prop: str) -> str:
    value = item.get(PROPERTY[prop])
    if prop == "IsHidden":
        return "true" if value else "false"
    return _text(value)


def apply(schema: dict, change: dict) -> list:
    """Edit ``schema`` in place; return one result per edit, as the companion reports them."""
    model = schema.get("model", schema)
    results = []
    for op in change["ops"]:
        if op["mode"] == "desktop":
            results.append({"seq": op["seq"], "status": "manual", "message": op.get("reason", "")})
            continue
        table, collection, item = _find(model, op)
        if op["op"] == "remove":
            if item is None:
                results.append({"seq": op["seq"], "status": "already", "message": "not in the model"})
            else:
                table[collection].remove(item)
                results.append({"seq": op["seq"], "status": "applied", "message": "removed"})
            continue
        if item is None:
            results.append({"seq": op["seq"], "status": "skipped", "message": f"{op['name']} is not in the model"})
            continue
        prop = op["property"]
        now_value = current(item, prop)
        if now_value == _text(op["new"]):
            results.append({"seq": op["seq"], "status": "already", "message": ""})
        elif now_value != _text(op["old"]):
            results.append({"seq": op["seq"], "status": "skipped", "message": f"{prop} changed since the request"})
        else:
            item[PROPERTY[prop]] = (op["new"] == "true") if prop == "IsHidden" else op["new"]
            results.append({"seq": op["seq"], "status": "applied", "message": ""})
    return results
