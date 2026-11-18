"""Build the workspace demo for the GitHub Pages site: docs/workspace-demo/.

It makes a workspace from made-up models (the same synthetic sample as the rest of the site), gives it two
weeks of history - saves, reviews, metrics, tiers, change requests - and records what the workspace's
pages ask its server. The demo page runs the real workspace page against those recordings, and acts out
the main actions in the browser. Nothing in it comes from a real .pbix.

    python scripts/build_workspace_demo.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dax_lineage.pbi_report import decode  # noqa: E402
from dax_lineage.workspace import Store, scan_folders  # noqa: E402
from dax_lineage.workspace import changes as CH  # noqa: E402
from dax_lineage.workspace import check as C  # noqa: E402
from dax_lineage.workspace import lineage as L  # noqa: E402
from dax_lineage.workspace import metrics as M  # noqa: E402
from dax_lineage.workspace import policy as P  # noqa: E402
from dax_lineage.workspace.app import make_server  # noqa: E402

OUT = ROOT / "docs" / "workspace-demo"
STATIC = ROOT / "dax_lineage" / "workspace" / "static"
DEMO = ROOT / "scripts" / "workspace-demo"
SHOWN_FOLDER = r"C:\Users\demo\OneDrive - Contoso\BI - Reports"
START = datetime(2026, 11, 2, 8, 30, tzinfo=timezone.utc).timestamp()


# -- a .pbit as Desktop would save it ------------------------------------------------------------------------

def measures(schema):
    return {m["name"]: (t, m) for t in schema["model"]["tables"] for m in t.get("measures", [])}


def write_pbit(source: Path, target: Path, edits) -> None:
    with zipfile.ZipFile(source) as z:
        files = {n: z.read(n) for n in z.namelist()}
    schema, layout = json.loads(decode(files["DataModelSchema"])), json.loads(decode(files["Report/Layout"]))
    for _, m in measures(schema).values():
        m["lineageTag"] = "tag-" + m["name"].lower().replace(" ", "-")
    for edit in edits:
        edit(schema, layout)
    files["DataModelSchema"] = json.dumps(schema).encode("utf-16-le")
    files["Report/Layout"] = json.dumps(layout).encode("utf-16-le")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in files.items():
            z.writestr(name, body)


def set_dax(name, expression):
    def edit(schema, layout):
        measures(schema)[name][1]["expression"] = expression
    return edit


def set_field(name, field, value):
    def edit(schema, layout):
        measures(schema)[name][1][field] = value
    return edit


def rename(old, new):
    def edit(schema, layout):
        for _, m in measures(schema).values():
            if m["name"] == old:
                m["name"] = new
            text = "\n".join(m["expression"]) if isinstance(m["expression"], list) else m["expression"]
            m["expression"] = text.replace(f"[{old}]", f"[{new}]")
        for section in layout["sections"]:
            for vc in section["visualContainers"]:
                for part in ("config", "filters"):
                    if isinstance(vc.get(part), str):
                        vc[part] = vc[part].replace(f'{old}"', f'{new}"')
    return edit


def remove_measure(name):
    def edit(schema, layout):
        table, m = measures(schema)[name]
        table["measures"].remove(m)
    return edit


def add_measure(table_of, name, expression):
    def edit(schema, layout):
        measures(schema)[table_of][0]["measures"].append(
            {"name": name, "expression": expression, "lineageTag": "tag-" + name.lower().replace(" ", "-")})
    return edit


def drop_visual(visual_name):
    def edit(schema, layout):
        for section in layout["sections"]:
            for vc in list(section["visualContainers"]):
                if json.loads(vc["config"])["name"] == visual_name:
                    section["visualContainers"].remove(vc)
    return edit


# -- two weeks of a team's work ------------------------------------------------------------------------

class Team:
    def __init__(self, tmp: Path, source: Path):
        self.folder = tmp / "BI - Reports"
        self.folder.mkdir()
        self.store = Store(tmp / "workspace.db")
        self.store.add_folder(self.folder)
        self.source = source
        self.edits: dict = {}

    def save(self, name: str, day: float, *edits) -> None:
        """A save in Desktop on ``day`` (days after 2 November), with these edits on top of the file's last."""
        self.edits.setdefault(name, []).extend(edits)
        target = self.folder / name
        write_pbit(self.source, target, self.edits[name])
        when = START + day * 86400
        os.utime(target, (when, when))

    def scan(self) -> None:
        scan_folders(self.store)

    def model(self, name: str) -> dict:
        return next(m for m in self.store.models() if m["name"] == name)

    def key(self, model: str, measure: str) -> str:
        return next(o["object_key"] for o in self.store.model(self.model(model)["id"])["objects"]
                    if o["kind"] == "measure" and o["name"] == measure and o["in_model"])


def history(team: Team) -> None:
    s = team.store
    # 2 November: four self-contained files, copied from one another over the years
    team.save("Sales.pbit", 0)
    team.save("Customer insights.pbit", 0.05)
    team.save("Sales by region.pbit", 0.1, set_dax("Gross Margin", "[Total Sales] - [Total Cost] * 1.02"))
    team.save("Board pack.pbit", 0.15, set_dax(
        "Total Sales", "CALCULATE ( SUM ( Sales[LineAmount] ), Sales[LineAmount] > 0 )"))
    team.scan()
    for name, owner, group, notes in (
            ("Sales", "Linh Tran", "Sales BI", "The sales model everyone builds on. Certified metrics live here."),
            ("Customer insights", "An Nguyen", "Customer analytics", "Segments, customer counts and share of sales."),
            ("Sales by region", "Minh Pham", "Regional sales", "Regional cut of Sales, refreshed nightly."),
            ("Board pack", "Hoa Le", "Finance", "Hoa's monthly board figures. Personal: not reviewed.")):
        s.describe_model(team.model(name)["id"], owner, group, notes)

    sales = team.model("Sales")["id"]
    total = M.create(s, sales, team.key("Sales", "Total Sales"), definition="Invoiced sales after discounts, "
                     "before tax and returns.", owner="Linh Tran", by="Linh Tran")
    M.certify(s, total["id"], by="Minh Pham", note="Agreed with Finance in the October close")
    margin = M.create(s, sales, team.key("Sales", "Gross Margin"), definition="Total Sales minus Total Cost.",
                      owner="Linh Tran", by="Linh Tran")
    M.certify(s, margin["id"], by="Minh Pham")
    M.accept_variant(s, total["id"], team.model("Board pack")["id"], team.key("Board pack", "Total Sales"),
                     "The board pack leaves out returns, as agreed with Finance.", by="Minh Pham")
    M.create(s, team.model("Customer insights")["id"], team.key("Customer insights", "Customer Count"),
             definition="Customers with at least one order in the period.", owner="An Nguyen", by="An Nguyen")

    P.set_tier(s, sales, "certified", by="Minh Pham")
    P.set_tier(s, team.model("Board pack")["id"], "personal", by="Minh Pham")

    s.annotate(sales, team.key("Sales", "Sales Rolling 3M"), owner="An Nguyen", decision="remove",
               note="Nobody has used it since the rolling view was dropped.", by="An Nguyen")
    s.annotate(sales, team.key("Sales", "Discount Impact"), owner="Linh Tran", decision="keep",
               note="Finance asks for it at quarter end.", by="Linh Tran")
    s.annotate(team.model("Customer insights")["id"], team.key("Customer insights", "Segment Share of Sales"),
               owner="An Nguyen", by="An Nguyen")
    C.accept(s, sales, team.key("Sales", "_Base Sales Helper"), "unused-measure",
             "A helper for the calculation group: nothing in the report reads it directly.", by="Linh Tran")

    # 5 November: Linh describes the key measures; Minh approves
    team.save("Sales.pbit", 3.1, set_field("Total Sales", "description", "Invoiced sales after discounts"),
              set_field("Gross Margin", "description", "Total Sales minus Total Cost"),
              set_field("Sales YTD", "description", "Total Sales from 1 January to the last date in context"))
    team.scan()
    v2 = team.model("Sales")["version_id"]
    s.add_comment(v2, "Descriptions for the three measures the board asks about.", by="Linh Tran")
    s.set_review(v2, "approved", by="Minh Pham", note="Thanks, reads well.")

    # 6 November: a change request to describe the time-intelligence measures, approved, waiting for Desktop
    request = CH.create(s, sales, "Describe the time-intelligence measures", [
        {"op": "set", "object_key": team.key("Sales", "Sales PY"), "field": "description",
         "value": "Total Sales for the same period last year"},
        {"op": "set", "object_key": team.key("Sales", "Sales MTD"), "field": "description",
         "value": "Total Sales from the first of the month"},
        {"op": "set", "object_key": team.key("Sales", "Sales QTD"), "field": "description",
         "value": "Total Sales from the start of the quarter"}], by="An Nguyen")
    CH.comment(s, request["id"], "Wording follows the glossary.", by="An Nguyen")
    CH.decide(s, request["id"], "approved", by="Linh Tran", note="Go ahead.")

    # 8 November: Customer insights - a change applied by the companion, confirmed by the next scan
    ci = team.model("Customer insights")["id"]
    request = CH.create(s, ci, "Describe Total Cost and hide the helper", [
        {"op": "set", "object_key": team.key("Customer insights", "Total Cost"), "field": "description",
         "value": "Cost of goods sold, before freight"},
        {"op": "set", "object_key": team.key("Customer insights", "_Base Sales Helper"), "field": "is_hidden",
         "value": True}], by="An Nguyen")
    CH.decide(s, request["id"], "approved", by="Minh Pham")
    ops = CH.get(s, request["id"])["ops"]
    CH.applied(s, request["id"], [{"seq": o["seq"], "status": "applied"} for o in ops], by="An Nguyen",
               machine="LAPTOP-AN")
    team.save("Customer insights.pbit", 6.2, set_field("Total Cost", "description", "Cost of goods sold, before freight"),
              set_field("_Base Sales Helper", "isHidden", True))

    # 9 November: Minh changes a ratio in Sales by region; Linh asks for DIVIDE
    team.save("Sales by region.pbit", 7.3, set_dax("Sales YoY %", "( [Total Sales] - [Sales PY] ) / [Sales PY]"))
    # 10 November: Hoa's own file, no review needed
    team.save("Board pack.pbit", 8.1, add_measure("Total Sales", "Board Sales Target", "[Total Sales] * 1.08"))
    team.scan()
    s.add_comment(team.model("Customer insights")["version_id"], "Applied from change request through the "
                  "companion.", by="An Nguyen")
    region = team.model("Sales by region")["version_id"]
    s.add_comment(region, "Last year is blank for new regions, so this divides by zero.", by="Linh Tran",
                  object_key=team.key("Sales by region", "Sales YoY %"))
    s.set_review(region, "changes_requested", by="Linh Tran", note="Use DIVIDE so a zero last year shows blank.")

    # 11 November: An proposes removing measures nobody uses
    unused = [o for o in s.model(ci)["objects"] if o["kind"] == "measure" and o["in_model"]
              and o["usage_status"] in ("unused", "deadChain")]
    items = []
    for o in unused:
        trial = items + [{"op": "remove", "object_key": o["object_key"], "reason": "unused in every report"}]
        if not CH.plan(s, ci, trial)["problems"]:
            items = trial
        if len(items) == 2:
            break
    if items:
        request = CH.create(s, ci, "", items, by="An Nguyen")
        CH.comment(s, request["id"], "Checked the Customers page: neither is on it.", by="An Nguyen")

    # 16 November: a big save of Sales that breaks a visual - the certified tier's policy stops it
    team.save("Sales.pbit", 14.2, rename("Total Sales", "Net Sales"),
              set_dax("Sales PY", "CALCULATE (\n    [Net Sales],\n    DATEADD ( 'Date'[Date], -1, YEAR )\n)"),
              set_field("Order Count", "formatString", "0.0"), remove_measure("Gross Margin %"),
              add_measure("Total Cost", "Sales Target", "[Net Sales] * 1.1"), drop_visual("slicer1"))
    team.scan()
    v3 = team.model("Sales")["version_id"]
    s.add_comment(v3, "Gross Margin % is still on the Overview card. Put it back, or change the card first?",
                  by="An Nguyen")


# -- recording what the pages ask -----------------------------------------------------------------------

def record(store: Store) -> dict:
    httpd = make_server(store, port=0, quiet=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    answers: dict = {}

    def get(path: str):
        if path in answers:
            return answers[path]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as response:
            answers[path] = json.loads(response.read())
        return answers[path]

    try:
        for path in ("/api/session", "/api/summary", "/api/models", "/api/folders", "/api/scan", "/api/standards",
                     "/api/rules/export", "/api/metrics", "/api/policies", "/api/account", "/api/admin/users",
                     "/api/admin/audit?limit=100&user="):
            get(path)
        for status in ("pending", "changes_requested", "approved", "all"):
            get(f"/api/reviews?status={status}")
        for status in ("open", "confirmed", "all"):
            for c in get(f"/api/changes?status={status}"):
                get(f"/api/changes/{c['id']}")
        for which in ("remove", "keep"):
            get(f"/api/decisions?decision={which}")
        for m in get("/api/models"):
            get(f"/api/models/{m['id']}")
        for m in get("/api/metrics")["metrics"]:
            get(f"/api/metrics/{m['id']}")
        for o in get("/api/standards")["owners"]:
            get(f"/api/standards/owner?name={quote(o['owner'] or '')}")
        with store.connect() as con:
            object_ids = [r["id"] for r in con.execute("SELECT id FROM object ORDER BY id")]
            versions = [dict(r) for r in con.execute(
                "SELECT id, model_id, number, status FROM model_version ORDER BY model_id, number")]
        for i in object_ids:
            get(f"/api/objects/{i}")
        for v in versions:
            get(f"/api/versions/{v['id']}")
            for earlier in versions:
                if earlier["model_id"] == v["model_id"] and earlier["number"] < v["number"] \
                        and earlier["status"] != "error":
                    get(f"/api/versions/{v['id']}?against={earlier['id']}")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return answers


def search_index(store: Store) -> list:
    """Every measure and column of every model's current version: the demo searches these in the browser."""
    with store.connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT o.id, o.kind, o.table_name, o.name, o.usage_status, o.in_model, o.display_folder, "
            "o.expression, m.id AS model_id, m.name AS model_name, m.kind AS model_kind, a.decision, a.owner "
            "FROM object o JOIN model m ON m.current_version_id = o.version_id "
            "LEFT JOIN annotation a ON a.model_id = m.id AND a.object_key = o.object_key "
            "WHERE m.missing = 0 AND o.kind IN ('measure', 'column') ORDER BY lower(o.name), lower(m.name)")]
    for r in rows:
        r["expression"], r["decision"], r["owner"] = r["expression"] or "", r["decision"] or "", r["owner"] or ""
    return rows


def graphs(store: Store, answers: dict) -> dict:
    out = {}
    with store.connect() as con:
        versions = [r["id"] for r in con.execute("SELECT id FROM model_version WHERE status = 'ok'")]
    for vid in versions:
        out[f"v{vid}"] = L.page_data(store, vid)
    for path in answers:
        if path.startswith("/api/versions/") and "?against=" in path:
            vid, against = path[len("/api/versions/"):].split("?against=")
            out[f"v{vid}-a{against}"] = L.page_data(store, int(vid), int(against))
        if path.startswith("/api/changes/") and path[len("/api/changes/"):].isdigit():
            change = answers[path]
            out[f"c{change['id']}"] = L.page_data(store, change["base_version_id"], change=CH.get(store, change["id"]))
    for key, data in out.items():
        data["back"] = "index.html" + data["back"].lstrip("/")
    return out


def tidy(value, folder: str, tmp: str):
    """Show the sample folder as it would look on a Windows PC, and drop the build's temporary paths."""
    if isinstance(value, dict):
        return {k: tidy(v, folder, tmp) for k, v in value.items()}
    if isinstance(value, list):
        return [tidy(v, folder, tmp) for v in value]
    if isinstance(value, str) and tmp in value:
        if folder in value:
            head, _, rest = value.partition(folder)
            return head + SHOWN_FOLDER + rest.replace("/", "\\")
        return value.replace(tmp, "C:\\PowerBIWorkspace").replace("/", "\\")
    return value


def build(out: Path = OUT) -> dict:
    spec = importlib.util.spec_from_file_location("make_report_samples", ROOT / "scripts" / "make_report_samples.py")
    samples = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(samples)
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name).resolve()
        samples.write_fixtures(tmp)
        team = Team(tmp, tmp / "sales.pbit")
        history(team)
        answers = record(team.store)
        index = search_index(team.store)
        pages = graphs(team.store, answers)
        answers["/api/folders"]["can_browse"] = False  # no folder dialog in a browser
        data = tidy({"answers": answers, "search": index}, str(team.folder), str(tmp))
        pages = tidy(pages, str(team.folder), str(tmp))

    if out.exists():
        shutil.rmtree(out)
    (out / "graphs").mkdir(parents=True)
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for key, page in pages.items():
        (out / "graphs" / f"{key}.json").write_text(json.dumps(page, ensure_ascii=False, separators=(",", ":")),
                                                    encoding="utf-8")
    for name in ("app.js", "app.css"):
        shutil.copyfile(STATIC / name, out / name)
    for name in ("index.html", "demo.js", "demo.css"):
        shutil.copyfile(DEMO / name, out / name)
    (out / "graph.html").write_text(graph_page(), encoding="utf-8")
    return {"answers": len(data["answers"]), "graphs": len(pages)}


def graph_page() -> str:
    """The offline explorer, opened on a recorded graph named in the address: graph.html?g=v12&focus=..."""
    page = L.packaged()
    # the explorer's own policy, with one change: it may read the demo's graph files from this site
    page = page.replace("connect-src 'none'", "connect-src 'self'", 1)
    boot = """<script>
/* workspace demo: the graph a workspace page linked to, from this site's recorded graphs */
(function () {
  var params = new URLSearchParams(location.search);
  var key = (params.get("g") || "").replace(/[^a-z0-9-]/gi, "");
  var $ = function (id) { return document.getElementById(id); };
  if (!key) return;
  fetch("graphs/" + key + ".json").then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (data) {
      $("landing").hidden = true;
      $("app").hidden = false;
      if ($("stats")) $("stats").hidden = false;
      var other = $("openAnother");
      if (other) {
        var back = document.createElement("a");
        back.href = data.back;
        back.textContent = "\\u2190 Back to the workspace demo";
        back.className = "back-link";
        other.replaceWith(back);
      }
      document.querySelectorAll("header .actions a.button").forEach(function (a) { a.hidden = true; });
      $("title").textContent = data.title;
      document.title = data.title + " \\u2014 lineage (demo)";
      LineageViewer.mount(data.graph, { subtitle: data.subtitle });
      var focus = params.get("focus");
      if (focus) setTimeout(function () { LineageViewer.select(focus); }, 50);
    })
    .catch(function () { /* unknown graph: the explorer's own landing page stays */ });
})();
</script>
"""
    index = page.rfind("</body>")
    return page[:index] + boot + page[index:]


if __name__ == "__main__":
    print(build())
