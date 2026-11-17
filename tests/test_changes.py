"""Team workspace, milestone 4: change requests, from proposal to the edit confirmed in a saved file."""
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest

from dax_lineage.cli import main as daxlin
from dax_lineage.pbi_report import decode
from dax_lineage.workspace import Store, scan_folders
from dax_lineage.workspace import changes as CH
from dax_lineage.workspace.app import make_server
from dax_lineage.workspace.apply_tmsl import apply
from test_review import pbit, rename, save  # noqa: F401  (pbit is a fixture)
from test_workspace import call

COMPANION = Path(__file__).resolve().parent.parent / "dax_lineage" / "workspace" / "companion"


@pytest.fixture
def ws(tmp_path, pbit):
    folder = tmp_path / "BI"
    folder.mkdir()
    store = Store(tmp_path / "ws.db")
    store.add_folder(folder)
    save(pbit, folder / "Sales.pbit")
    scan_folders(store)
    model = store.models()[0]
    keys = {o["name"]: o["object_key"] for o in store.model(model["id"])["objects"] if o["kind"] == "measure"}
    return store, model, keys, folder / "Sales.pbit"


def remove(key):
    return {"op": "remove", "object_key": key}


def desktop_saves(pbix: Path, change: dict, also=None, bump=60) -> list:
    """What happens on the author's PC: the companion edits the open model, the author saves."""
    with zipfile.ZipFile(pbix) as z:
        files = {n: z.read(n) for n in z.namelist()}
    schema, layout = json.loads(decode(files["DataModelSchema"])), json.loads(decode(files["Report/Layout"]))
    results = apply(schema, change)
    if also:
        also(schema, layout)
    files["DataModelSchema"] = json.dumps(schema).encode("utf-16-le")
    files["Report/Layout"] = json.dumps(layout).encode("utf-16-le")
    with zipfile.ZipFile(pbix, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in files.items():
            z.writestr(name, body)
    st = pbix.stat()
    os.utime(pbix, (st.st_atime + bump, st.st_mtime + bump))
    return results


def ready_for(store, request_id):
    return next(c for c in CH.ready(store) if c["id"] == request_id)


# -- what a request may contain -------------------------------------------------------

def test_removal_refused_while_something_reads_it(ws):
    store, model, keys, _ = ws
    planned = CH.plan(store, model["id"], [remove(keys["Total Quantity"])])
    assert planned["problems"] and "Average Unit Price" in planned["problems"][0]
    # a dead chain goes together: Total Quantity is read only by these unused measures
    chain = [remove(keys[n]) for n in ("Total Quantity", "Average Unit Price", "Units per Order")]
    planned = CH.plan(store, model["id"], chain)
    assert not planned["problems"] and [o["op"] for o in planned["ops"]] == ["remove"] * 3


def test_removal_refused_while_a_visual_shows_it(ws):
    store, model, keys, _ = ws
    problems = CH.plan(store, model["id"], [remove(keys["Sales YoY %"])])["problems"]
    assert problems and "take it out of those visuals" in problems[0]


def test_warnings_for_keep_decisions_and_other_reports(ws, tmp_path, pbit):
    store, model, keys, pbix = ws
    store.annotate(model["id"], keys["Sales QTD"], decision="keep", by="Hoa")
    import shutil
    shutil.copy(Path(pbit).parent / "sales.pbix", pbix.parent / "Thin report.pbix")
    scan_folders(store)
    warnings = CH.plan(store, model["id"], [remove(keys["Sales QTD"]), remove(keys["Discount Impact"])])["warnings"]
    assert any("marked keep by Hoa" in w for w in warnings)
    assert not any("Sales QTD" in w and "Thin report" in w for w in warnings)  # the report does not use it
    rename_warning = CH.plan(store, model["id"], [{"op": "rename", "object_key": keys["Total Sales"], "value": "Net"}])
    assert any("Thin report uses a field named Total Sales" in w for w in rename_warning["warnings"])


def test_property_edits(ws):
    store, model, keys, _ = ws
    planned = CH.plan(store, model["id"], [
        {"op": "set", "object_key": keys["Sales YTD"], "field": "description", "value": "  Year to date.  "},
        {"op": "set", "object_key": keys["Sales YTD"], "field": "is_hidden", "value": True},
        {"op": "set", "object_key": keys["Sales YTD"], "field": "display_folder",
         "value": "Time intelligence\\Period to date"},                                         # already so
    ])
    assert [(o["field"], o["old_value"], o["new_value"]) for o in planned["ops"]] == [
        ("description", "", "Year to date."), ("is_hidden", "false", "true")]
    bad = CH.plan(store, model["id"], [{"op": "set", "object_key": keys["Sales YTD"], "field": "expression", "value": "1"}])
    assert bad["problems"] and "cannot set expression" in bad["problems"][0]
    twice = CH.plan(store, model["id"], [remove(keys["Sales QTD"]),
                                         {"op": "set", "object_key": keys["Sales QTD"], "field": "description", "value": "x"}])
    assert "removed and edited" in twice["problems"][0]
    assert CH.plan(store, model["id"], [])["problems"] == ["nothing would change"]
    first = CH.create(store, model["id"], "", [remove(keys["Sales QTD"])], by="An")
    assert f"Sales QTD is already in open change #{first['id']}" in CH.plan(store, model["id"], [remove(keys["Sales QTD"])])["warnings"]
    with pytest.raises(KeyError):
        CH.plan(store, 999, [])


def test_renames(ws):
    store, model, keys, _ = ws
    # nothing shows Customer Count directly: the companion renames it and rewrites the DAX that uses it
    planned = CH.plan(store, model["id"], [{"op": "rename", "object_key": keys["Customer Count"], "value": "Customers"}])
    ops = planned["ops"]
    assert [(o["op"], o["mode"]) for o in ops] == [("rename", "companion"), ("expression", "companion")]
    assert ops[1]["name"] == "Sales per Customer" and ops[1]["new_value"] == "DIVIDE ( [Total Sales], [Customers] )"
    # visuals show Gross Margin: left for Desktop, which updates visuals and DAX itself
    planned = CH.plan(store, model["id"], [{"op": "rename", "object_key": keys["Gross Margin"], "value": "Margin"}])
    assert [(o["op"], o["mode"]) for o in planned["ops"]] == [("rename", "desktop")]
    clash = CH.plan(store, model["id"], [{"op": "rename", "object_key": keys["Customer Count"], "value": "total sales"}])
    assert "already has the name" in clash["problems"][0]
    assert CH.plan(store, model["id"], [{"op": "rename", "object_key": keys["Customer Count"], "value": " "}])["problems"]


def test_rewrite_reference():
    r = CH.rewrite_reference
    assert r("[A] + 'T'[A] + U[A] + [AB]", "A", "B", "T") == "[B] + 'T'[B] + U[A] + [AB]"
    assert r("[a] // [A] in a comment", "A", "B", "T") == "[B] // [A] in a comment"
    assert r('"[A]" & [A]', "A", "B", "T") == '"[A]" & [B]'
    assert r("[A]", "A", "x]y", "T") == "[x]]y]"
    assert r("[Odd]]Name]", "Odd]Name", "Even", "T") == "[Even]"


# -- from proposal to confirmation ----------------------------------------------------

def test_a_change_from_proposal_to_confirmation(ws, pbit):
    store, model, keys, pbix = ws
    items = [remove(keys["Sales QTD"]), remove(keys["Sales MTD"]),
             {"op": "set", "object_key": keys["Sales YTD"], "field": "description", "value": "Year to date."},
             {"op": "rename", "object_key": keys["Customer Count"], "value": "Customers"},
             {"op": "rename", "object_key": keys["Gross Margin"], "value": "Margin"}]
    request = CH.create(store, model["id"], "", items, by="Linh")
    assert request["status"] == "proposed" and request["title"].startswith("Remove 2 measures")
    with pytest.raises(ValueError):
        CH.applied(store, request["id"], [], by="x")                    # not approved yet
    with pytest.raises(ValueError):
        CH.decide(store, request["id"], "applied")                      # not a decision
    CH.decide(store, request["id"], "approved", by="Minh")
    change = ready_for(store, request["id"])
    assert {o["property"] for o in change["ops"]} == {"", "Description", "Name", "Expression"}

    results = desktop_saves(pbix, change)
    assert [r["status"] for r in results] == ["applied", "applied", "applied", "applied", "manual", "applied"]
    request = CH.applied(store, request["id"], results, by="Linh", machine="PC-17")
    assert request["status"] == "applied" and "PC-17" in request["events"][-1]["body"]

    summary = scan_folders(store)
    request = CH.get(store, request["id"])
    assert request["status"] == "applied"                              # the rename by hand is not done yet
    assert [o["status"] for o in request["ops"]] == ["confirmed"] * 4 + ["manual", "confirmed"]
    assert request["events"][-1]["kind"] == "partly" and summary["new_versions"] == 1

    # the author renames Gross Margin in Desktop's Data pane, which also rewrites visuals and DAX, and saves
    desktop_saves(pbix, {"ops": []}, also=lambda s, l: rename(s, l, "Gross Margin", "Margin"), bump=120)
    summary = scan_folders(store)
    request = CH.get(store, request["id"])
    assert request["status"] == "confirmed" and request["confirmed_number"] == 3
    assert any("is confirmed" in line for line in summary["log"])
    assert CH.requests(store) == [] and CH.requests(store, "confirmed")[0]["id"] == request["id"]


def test_done_by_hand_is_confirmed_too(ws):
    store, model, keys, pbix = ws
    request = CH.create(store, model["id"], "Describe", [
        {"op": "set", "object_key": keys["Sales YTD"], "field": "description", "value": "YTD"}], by="An")
    CH.decide(store, request["id"], "approved", by="Minh")
    desktop_saves(pbix, ready_for(store, request["id"]))                # as if typed in Desktop: no report back
    scan_folders(store)
    assert CH.get(store, request["id"])["status"] == "confirmed"


def test_stale_requests_and_skipped_edits(ws, pbit):
    store, model, keys, pbix = ws
    request = CH.create(store, model["id"], "", [
        {"op": "set", "object_key": keys["Sales YTD"], "field": "description", "value": "From the request"}], by="An")
    # someone else describes it differently and saves before the request is approved
    def other(schema, layout):
        for t in schema["model"]["tables"]:
            for m in t.get("measures", []):
                if m["name"] == "Sales YTD":
                    m["description"] = "Typed in Desktop"
    desktop_saves(pbix, {"ops": []}, also=other)
    scan_folders(store)
    stale = CH.get(store, request["id"])["stale"]
    assert stale and "changed since" in stale[0]["why"]
    with pytest.raises(ValueError):
        CH.decide(store, request["id"], "approved", by="Minh")
    # the companion guards the same way when the model changes after approval
    change = {"ops": [dict(ready_for_shape(request), old="", new="From the request")]}
    with zipfile.ZipFile(pbix) as z:
        schema = json.loads(decode(z.read("DataModelSchema")))
    assert apply(schema, change)[0]["status"] == "skipped"
    CH.decide(store, request["id"], "cancelled", by="An")
    assert CH.get(store, request["id"])["status"] == "cancelled"


def ready_for_shape(request):
    op = request["ops"][0]
    return {"seq": op["seq"], "op": op["op"], "mode": op["mode"], "kind": op["kind"], "table": op["table_name"],
            "name": op["name"], "lineage_tag": op["lineage_tag"], "property": "Description"}


def test_apply_reports_already_done_and_missing(ws):
    store, model, keys, pbix = ws
    request = CH.create(store, model["id"], "", [remove(keys["Sales QTD"]),
                                                {"op": "set", "object_key": keys["Sales YTD"], "field": "is_hidden",
                                                 "value": True}], by="An")
    CH.decide(store, request["id"], "approved", by="Minh")
    change = ready_for(store, request["id"])
    with zipfile.ZipFile(pbix) as z:
        schema = json.loads(decode(z.read("DataModelSchema")))
    assert [r["status"] for r in apply(schema, change)] == ["applied", "applied"]
    assert [r["status"] for r in apply(schema, change)] == ["already", "already"]   # running it twice is harmless
    with pytest.raises(ValueError):
        CH.applied(store, request["id"], [{"seq": 9, "status": "applied"}])
    with pytest.raises(ValueError):
        CH.applied(store, request["id"], [{"seq": 1, "status": "exploded"}])
    failed = CH.applied(store, request["id"], [{"seq": 1, "status": "failed", "message": "Desktop refused"},
                                               {"seq": 2, "status": "failed", "message": "Desktop refused"}])
    assert failed["status"] == "approved" and failed["events"][-1]["kind"] == "apply-failed"


# -- the web API and the companion's calls --------------------------------------------

def test_api(ws):
    store, model, keys, _ = ws
    httpd = make_server(store, port=0, quiet=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        items = [remove(keys["Sales QTD"])]
        status, planned, _ = call(port, "POST", "/api/changes/preview", {"model_id": model["id"], "items": items})
        assert status == 200 and planned["ops"] and "model" not in planned
        status, error, _ = call(port, "POST", "/api/changes", {"model_id": model["id"], "items": [remove(keys["Total Sales"])]})
        assert status == 400 and error["problems"]
        assert call(port, "POST", "/api/changes", {"model_id": model["id"], "items": "x"})[0] == 400
        status, made, _ = call(port, "POST", "/api/changes", {"model_id": model["id"], "items": items, "by": "Linh"})
        assert status == 200 and made["status"] == "proposed"
        assert call(port, "GET", "/api/summary")[1]["open_changes"] == 1
        assert call(port, "POST", f"/api/changes/{made['id']}/decision", {"status": "approved"},
                    {"Origin": "http://evil.example"})[0] == 403
        assert call(port, "POST", f"/api/changes/{made['id']}/decision", {"status": "approved", "by": "Minh"})[0] == 200
        assert call(port, "POST", f"/api/changes/{made['id']}/comments", {"body": "Go", "by": "Minh"})[0] == 200
        # the companion: no Origin header, as a script sends none
        status, ready, _ = call(port, "GET", "/api/changes/ready")
        assert status == 200 and [c["id"] for c in ready["changes"]] == [made["id"]]
        assert call(port, "POST", f"/api/changes/{made['id']}/applied", {"results": "x"})[0] == 400
        status, done, _ = call(port, "POST", f"/api/changes/{made['id']}/applied",
                               {"results": [{"seq": 1, "status": "applied"}], "by": "Linh", "machine": "PC"})
        assert status == 200 and done["status"] == "applied"
        assert call(port, "GET", "/api/changes/99999")[0] == 404
        assert call(port, "GET", "/api/changes?status=all")[1][0]["id"] == made["id"]
    finally:
        httpd.shutdown()
        httpd.server_close()
    readonly = make_server(store, port=0, quiet=True, read_only=True)
    threading.Thread(target=readonly.serve_forever, daemon=True).start()
    try:
        assert call(readonly.server_address[1], "POST", "/api/changes", {"model_id": 1, "items": []})[0] == 403
    finally:
        readonly.shutdown()
        readonly.server_close()


def test_cli_and_companion_files(ws, tmp_path, capsys):
    store, model, keys, _ = ws
    CH.create(store, model["id"], "Tidy", [remove(keys["Sales QTD"])], by="Linh")
    db = ["workspace", "--db", str(store.path)]
    assert daxlin(db + ["requests"]) == 0
    assert "Tidy" in capsys.readouterr().out
    out = tmp_path / "companion"
    assert daxlin(db + ["companion", "--out", str(out)]) == 0
    script = (out / "PowerBIWorkspaceCompanion.ps1").read_bytes()
    assert script.startswith(b"\xef\xbb\xbf") and b"\r\n" in script and b"\r\r\n" not in script
    tool = json.loads((out / "PowerBIWorkspace.pbitool.template.json").read_text())
    assert tool["iconData"].startswith("image/png;base64,") and tool["path"].endswith("powershell.exe")


def test_companion_script_matches_the_api():
    text = (COMPANION / "PowerBIWorkspaceCompanion.ps1").read_text()
    assert text.isascii()  # Windows PowerShell 5.1 would misread anything else in a file without a BOM
    for needle in ('"/api/changes/ready"', '"/api/changes/{0}/applied"', ".changes)", "SaveChanges()",
                   "UndoLocalChanges()", "SerializeDatabase($db)", '-cne "APPLY"', "LineageTag"):
        assert needle in text, needle
    for status in ("applied", "already", "skipped", "manual", "failed"):
        assert f'"{status}"' in text and status in CH.RESULTS
    # every property the server may send is one the script sets
    properties = {"Name", "Expression"} | {tom for _, tom, _ in CH.FIELDS.values()}
    assert properties <= {"Name", "Expression", "Description", "DisplayFolder", "FormatString", "IsHidden"}
    installer = (COMPANION / "install-companion.ps1").read_text()
    assert installer.isascii() and "%server%" in installer and "Microsoft.AnalysisServices.retail.amd64" in installer
