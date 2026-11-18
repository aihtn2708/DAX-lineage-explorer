"""The lineage explorer's graph for any version the workspace has stored, and the page that shows it.

Every scan stores a model's objects, dependencies, visuals and usage. From
those this rebuilds the same graph the explorer draws for a file you open in
it, so the explorer's page can show any version, of any model, from the
workspace, with nothing read from the .pbix again.

Nodes can carry a ``mark`` the explorer draws on them: what a review found
changed (added, edited, renamed, broken), or what a change request touches.
"""

from __future__ import annotations

import json
from typing import Optional

from ..dax import scan
from ..graph import Edge, Lineage, Node, pretty_type
from ..offline import CSP, _inline_safe, packaged
from .store import Store

MARKS = ("added", "edited", "renamed", "broken", "touched", "follows")


def _norm(text: str) -> str:
    return (text or "").strip().casefold()


def graph_for_version(store: Store, version_id: int, marks: Optional[dict] = None) -> Optional[dict]:
    """The explorer's graph (as ``Lineage.to_dict``) for a stored version; ``marks`` maps node id to a mark."""
    version = store.version(version_id)
    if version is None:
        return None
    rows = store.version_rows(version_id)
    objects = rows["objects"]
    measure_names = {_norm(o["name"]) for o in objects if o["kind"] == "measure" and o["in_model"]}
    column_names = {_norm(o["name"]) for o in objects if o["kind"] == "column" and o["in_model"]}
    table_names = {_norm(o["name"]) for o in objects if o["kind"] == "table" and o["in_model"]}

    lineage = Lineage(has_report=bool(version["has_report"] or rows["usages"]))
    for o in objects:
        label = (o["name"] if o["kind"] in ("measure", "table") or not o["table_name"]
                 else f"{o['table_name']}[{o['name']}]")
        node = Node(id=o["node"], kind=o["kind"], name=o["name"], table=o["table_name"] if o["kind"] != "table" else "",
                    label=label, expression=o["expression"] or "", description=o["description"] or "",
                    display_folder=o["display_folder"] or "", format_string=o["format_string"] or "",
                    data_type=o["data_type"] or "", is_hidden=bool(o["is_hidden"]),
                    is_calculated=bool(o.get("is_calculated")), state=o["state"] or "",
                    error_message=o["error_message"] or "", exists=bool(o["in_model"]), usage=o["usage"])
        if node.expression:
            found = scan(node.expression, measure_names, column_names, table_names)
            node.functions = sorted(set(found.functions))
            node.time_intelligence = found.time_intelligence
            node.iterators = found.iterators
            node.context_modifiers = found.context_modifiers
            node.variables = found.variables
            node.unresolved = sorted({r.name for r in found.unresolved})
        lineage.nodes[node.id] = node
    for v in rows["visuals"]:
        title = v["title"] or pretty_type(v["visual_type"])
        lineage.nodes[v["node"]] = Node(id=v["node"], kind="visual", name=title, table=v["page"],
                                        label=f"{v['page']} › {title}", report=v["report"], page=v["page"],
                                        page_order=v["page_order"], visual_type=v["visual_type"] or "",
                                        title=v["title"] or "")
    for d in rows["dependencies"]:
        if d["source"] in lineage.nodes and d["target"] in lineage.nodes:
            lineage.edges.append(Edge(d["source"], d["target"], d["kind"], bool(d.get("via_variable"))))
    for u in rows["usages"]:
        if u["visual"] in lineage.nodes and u["field"] in lineage.nodes:
            lineage.edges.append(Edge(u["visual"], u["field"], "used_in_visual", roles=list(u["roles"])))

    lineage.compute_depth()
    deepest = max([n.depth for n in lineage.nodes.values() if n.kind != "visual"], default=0)
    for node in lineage.nodes.values():
        if node.kind == "visual":
            node.depth = deepest + 1
    graph = lineage.to_dict()
    for node in graph["nodes"]:
        mark = (marks or {}).get(node["id"])
        if mark in MARKS:
            node["mark"] = mark
    return graph


def review_marks(store: Store, version_id: int, against_id: Optional[int] = None) -> dict:
    """Node marks for what a version changed since the one before (or ``against_id``)."""
    from .diff import compare
    review = compare(store, version_id, against_id)
    if review["baseline"]:
        return {}
    by_key = {}
    missing = {}
    for o in store.version_rows(version_id)["objects"]:
        if o["in_model"]:
            by_key[o["object_key"]] = o["node"]
        else:
            label = o["name"] if o["kind"] == "measure" or not o["table_name"] else f"{o['table_name']}[{o['name']}]"
            missing[label] = o["node"]
    marks = {}
    for item in review["added"]:
        if item["key"] in by_key:
            marks[by_key[item["key"]]] = "added"
    for item in review["edited"]:
        if item["key"] in by_key:
            marks[by_key[item["key"]]] = "follows" if item.get("follows_rename") else "edited"
    for item in review["renamed"]:
        if item["key"] in by_key:
            marks[by_key[item["key"]]] = "renamed"
    for item in review["broken"]:
        if item["label"] in missing:
            marks[missing[item["label"]]] = "broken"
    return marks


def change_marks(store: Store, request: dict) -> dict:
    """Node marks for what a change request touches, in the version it was made against."""
    by_key = {o["object_key"]: o["node"] for o in store.version_rows(request["base_version_id"])["objects"]
              if o["in_model"]}
    return {by_key[op["object_key"]]: "follows" if op["op"] == "expression" else "touched"
            for op in request["ops"] if op["object_key"] in by_key}


def explorer_page(graph: dict, title: str, subtitle: str, focus: str = "", back: str = "/") -> str:
    """The offline explorer with ``graph`` built in, opened on it, and a way back to the workspace."""
    page = packaged()
    # model text (DAX, names, titles) inside a script element: no "<" may reach the HTML parser
    payload = _inline_safe(json.dumps({"graph": graph, "title": title, "subtitle": subtitle, "focus": focus,
                                       "back": back}, ensure_ascii=False).replace("<", "\\u003c"))
    boot = f"""<script type="application/json" id="workspaceGraph">{payload}</script>
<script>
/* opened from the Power BI workspace: show its graph instead of the landing page */
(function () {{
  var data = JSON.parse(document.getElementById("workspaceGraph").textContent);
  var $ = function (id) {{ return document.getElementById(id); }};
  $("landing").hidden = true;
  $("app").hidden = false;
  if ($("stats")) $("stats").hidden = false;
  var other = $("openAnother");
  if (other) {{
    var back = document.createElement("a");
    back.href = data.back;
    back.textContent = "\\u2190 Back to the workspace";
    back.className = "back-link";
    other.replaceWith(back);
  }}
  // the site's own links (sample download, GitHub) are not what someone in the workspace needs
  document.querySelectorAll("header .actions a.button").forEach(function (a) {{ a.hidden = true; }});
  $("title").textContent = data.title;
  document.title = data.title + " \\u2014 lineage";
  LineageViewer.mount(data.graph, {{ subtitle: data.subtitle }});
  if (data.focus) setTimeout(function () {{ LineageViewer.select(data.focus); }}, 50);
}})();
</script>
"""
    index = page.rfind("</body>")
    return page[:index] + boot + page[index:]


def page_data(store: Store, version_id: int, against: Optional[int] = None,
              change: Optional[dict] = None) -> Optional[dict]:
    """The graph of a version with what a review or a change marks, its title, and where "back" goes."""
    version = store.version(version_id)
    if version is None:
        return None
    if change is not None:
        marks = change_marks(store, change)
        what = f"change #{change['id']}: {len(marks)} object(s) it touches are marked"
        back = f"/#/change/{change['id']}"
    elif against is not None or version["review"] not in ("baseline", "unreadable"):
        marks = review_marks(store, version_id, against)
        what = f"{len(marks)} object(s) changed since the version before are marked" if marks else "no changes marked"
        back = f"/#/version/{version_id}"
    else:
        marks, what, back = {}, "", f"/#/model/{version['model_id']}"
    graph = graph_for_version(store, version_id, marks)
    stats = graph["stats"]
    report = stats.get("report")
    subtitle = (f"{stats['measures']} measures · {stats['columns']} columns · {stats['edges']} dependencies"
                + (f" · {report['visuals']} visuals" if report else "") + (f" · {what}" if what else ""))
    return {"graph": graph, "title": f"{version['model_name']} v{version['number']}", "subtitle": subtitle,
            "back": back}


def page_for(store: Store, version_id: int, focus: str = "", against: Optional[int] = None,
             change: Optional[dict] = None) -> Optional[str]:
    data = page_data(store, version_id, against, change)
    if data is None:
        return None
    return explorer_page(data["graph"], data["title"], data["subtitle"], focus, data["back"])


def page_headers() -> dict:
    """The explorer page's own policy: inline code allowed, and no request to anywhere, not even the workspace."""
    return {"Content-Security-Policy": CSP + "; frame-ancestors 'none'"}

