"""Live lineage: the watcher's snapshots in, what the second screen shows out."""
import copy
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

from dax_lineage import live as L
from dax_lineage.pbi_report import read_report

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    """The site's sample model as the watcher would send it, and its saved report file."""
    spec = importlib.util.spec_from_file_location("samples", ROOT / "scripts" / "make_report_samples.py")
    samples = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(samples)
    folder = tmp_path_factory.mktemp("live")
    samples.write_fixtures(folder)
    pbit = folder / "sales.pbit"
    rows = read_report(pbit).model
    names = {t["ID"]: t["Name"] for t in rows["tables"]}
    snapshot = {"source": "localhost:51542", "name": "Sales", "file": str(pbit),
                "tables": [{"Name": t["Name"]} for t in rows["tables"]],
                "measures": [{**{k: v for k, v in m.items() if k != "TableID"}, "Table": names[m["TableID"]],
                              "LineageTag": m.get("LineageTag") or "tag-" + m["Name"],
                              "ModifiedTime": "2026-11-22T06:00:00Z"} for m in rows["measures"]],
                "columns": [{**{k: v for k, v in c.items() if k != "TableID"}, "Table": names[c["TableID"]]}
                            for c in rows["columns"]],
                "relationships": rows.get("relationships") or []}
    return snapshot


def measure(snapshot, name):
    return next(m for m in snapshot["measures"] if m["Name"] == name)


def edit(snapshot, name, at="2026-11-22T06:05:00Z", **changes):
    out = copy.deepcopy(snapshot)
    m = measure(out, name)
    m.update(changes)
    m["ModifiedTime"] = at
    return out


def test_connecting_then_following_an_edit(sample):
    live = L.Live()
    first = live.receive(sample)
    assert first["kind"] == "connected" and first["model"]["name"] == "Sales"
    assert first["model"]["measures"] == len(sample["measures"]) and first["model"]["visuals"] > 0
    assert live.receive(copy.deepcopy(sample)) is None                      # nothing changed: nothing to show

    event = live.receive(edit(sample, "Total Sales", Expression="SUMX ( Sales, Sales[LineAmount] )"))
    focus = event["focus"]
    assert event["kind"] == "change" and focus["name"] == "Total Sales" and focus["change"] == "edited"
    assert focus["before"].startswith("SUM") and focus["after"] == "SUMX ( Sales, Sales[LineAmount] )"
    reads = {u["name"]: u for u in focus["upstream"]}
    assert reads["LineAmount"]["level"] == 1 and reads["Sales"]["kind"] == "table"
    assert reads["UnitPrice"]["level"] == 2                                 # LineAmount is a calculated column
    readers = {d["name"]: d for d in focus["downstream"]}
    assert "Gross Margin" in readers and readers["Gross Margin"]["level"] == 1
    assert any(d["level"] > 1 and d["via"] for d in focus["downstream"])     # readers of readers, and through what
    assert focus["visuals"] and any(v["direct"] for v in focus["visuals"])
    assert all(w["level"] != "error" for w in focus["warnings"])


def test_an_edit_that_breaks_its_readers(sample):
    live = L.Live()
    live.receive(sample)
    broken = edit(sample, "Total Cost", Expression="SUM ( Sales[Costs] ) + [Freight]", State="SemanticError",
                  ErrorMessage="The value for 'Costs' cannot be determined.")
    measure(broken, "Gross Margin").update(State="SemanticError", ErrorMessage="Total Cost is not valid.")
    focus = live.receive(broken)["focus"]
    texts = " | ".join(w["text"] for w in focus["warnings"])
    assert "does not work: The value for 'Costs'" in texts
    assert "[Freight]" in texts                                             # reads a measure that does not exist
    assert "stopped working: Gross Margin" in texts
    gross = next(d for d in focus["downstream"] if d["name"] == "Gross Margin")
    assert gross["newError"] and gross["error"] == "Total Cost is not valid."
    # fixed again: the same measure, now working
    fixed = live.receive(edit(sample, "Total Cost", at="2026-11-22T06:07:00Z"))["focus"]
    assert fixed["name"] == "Total Cost" and not [w for w in fixed["warnings"] if w["level"] == "error"]


def test_renames_removals_and_which_change_is_shown(sample):
    live = L.Live()
    live.receive(sample)
    renamed = live.receive(edit(sample, "Total Sales", Name="Net Sales"))["focus"]
    assert renamed["change"] == "renamed" and renamed["oldName"] == "Total Sales" and renamed["name"] == "Net Sales"

    before = live.models[sample["source"]].snapshot
    removed = copy.deepcopy(before)
    removed["measures"] = [m for m in removed["measures"] if m["Name"] != "Gross Margin"]
    focus = live.receive(removed)["focus"]
    assert focus["change"] == "removed" and focus["name"] == "Gross Margin" and focus["after"] == ""
    assert focus["warnings"][0]["level"] == "error" and "now have nothing to read" in focus["warnings"][0]["text"]

    # a DAX edit wins over a format change saved at the same time; the newest DAX edit wins over an older one
    both = edit(removed, "Total Quantity", at="2026-11-22T06:10:00Z", FormatString="0.0")
    both = edit(both, "Total Cost", at="2026-11-22T06:09:00Z", Expression="SUM ( Sales[Cost] ) * 1")
    both = edit(both, "Net Sales", at="2026-11-22T06:08:00Z", Expression="SUM ( Sales[LineAmount] ) * 1")
    event = live.receive(both)
    assert event["focus"]["name"] == "Total Cost"
    assert {c["name"]: c["change"] for c in event["changes"]} == {"Total Quantity": "format", "Total Cost": "edited",
                                                                   "Net Sales": "edited"}


def test_calculated_columns_and_looking_things_up(sample):
    live = L.Live()
    snap = copy.deepcopy(sample)
    snap["columns"].append({"Table": "Sales", "ExplicitName": "Margin", "Expression": "Sales[LineAmount] - Sales[Cost]",
                            "Type": "Calculated", "LineageTag": "tag-col-margin"})
    live.receive(snap)
    changed = copy.deepcopy(snap)
    changed["columns"][-1].update(Expression="Sales[LineAmount] - Sales[Cost] - Sales[Freight]",
                                  ModifiedTime="2026-11-22T06:20:00Z")
    focus = live.receive(changed)["focus"]
    assert focus["kind"] == "column" and focus["name"] == "Margin" and focus["change"] == "edited"
    hits = live.find(sample["source"], "gross")
    assert hits and hits[0]["name"].startswith("Gross")
    looked = live.explore(sample["source"], hits[0]["id"])
    assert looked["found"] and looked["upstream"]
    with pytest.raises(KeyError):
        live.explore("elsewhere", hits[0]["id"])


def test_saved_file_for_visuals(sample, tmp_path):
    live = L.Live()
    no_file = {**copy.deepcopy(sample), "file": ""}
    assert live.receive(no_file)["model"]["visuals"] is None
    with pytest.raises(ValueError):
        live.set_report(sample["source"], str(tmp_path / "nope.pbix"))
    state = live.set_report(sample["source"], sample["file"])
    assert state["models"][0]["visuals"] > 0 and "sales.pbit" in state["models"][0]["reportNote"]
    with pytest.raises(ValueError):
        live.receive({"measures": "not a list"})


# -- the server ----------------------------------------------------------------------------------------

def call(port, method, path, body=None, headers=None, host=None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **(headers or {})})
    if host:
        request.add_unredirected_header("Host", host)
    try:
        with urllib.request.urlopen(request, timeout=10) as r:
            raw = r.read()
            return r.status, dict(r.headers), raw
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


@pytest.fixture()
def server():
    live = L.Live()
    httpd = L.make_server(live, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.stopping = True
    with live.lock:
        live.lock.notify_all()
    httpd.shutdown()
    httpd.server_close()


def test_server_pages_token_and_host(server, sample):
    port, token = server.server_address[1], server.token
    status, headers, page = call(port, "GET", "/")
    assert status == 200 and b"live.js" in page and "script-src 'self'" in headers["Content-Security-Policy"]
    assert call(port, "GET", "/live.js")[0] == 200 and call(port, "GET", "/live.css")[0] == 200
    assert json.loads(call(port, "GET", "/api/hello")[2])["app"] == "daxlin-live"
    assert call(port, "GET", "/api/state", host="evil.example:80")[0] == 421       # DNS rebinding
    # posting needs the token
    assert call(port, "POST", "/api/snapshot", sample)[0] == 403
    assert call(port, "POST", "/api/snapshot", sample, {"X-Live-Token": "wrong"})[0] == 403
    status, _, raw = call(port, "POST", "/api/snapshot", sample, {"X-Live-Token": token})
    assert status == 200 and json.loads(raw) == {"changed": True, "seq": 1}
    assert json.loads(call(port, "POST", "/api/snapshot", sample, {"X-Live-Token": token})[2])["changed"] is False
    state = json.loads(call(port, "GET", "/api/state")[2])
    assert state["token"] == token and state["models"][0]["name"] == "Sales" and state["seq"] == 1
    hits = json.loads(call(port, "GET", "/api/find?source=localhost:51542&q=total")[2])
    assert hits and json.loads(call(port, "GET", f"/api/explore?source=localhost:51542&id={quote(hits[0]['id'])}")[2])["found"]
    assert call(port, "GET", "/api/explore?source=nope&id=x")[0] == 404
    assert call(port, "POST", "/api/report", {"source": "localhost:51542", "path": "/no/such.pbix"},
                {"X-Live-Token": token})[0] == 400
    assert call(port, "POST", "/api/snapshot", {"measures": 3}, {"X-Live-Token": token})[0] == 400


def test_server_pushes_each_change(server, sample):
    port, token = server.server_address[1], server.token
    call(port, "POST", "/api/snapshot", sample, {"X-Live-Token": token})
    got = []

    def listen():
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/events?after=1", timeout=10) as stream:
            for line in stream:
                if line.startswith(b"data: "):
                    got.append(json.loads(line[6:]))
                    return

    reader = threading.Thread(target=listen)
    reader.start()
    call(port, "POST", "/api/snapshot", edit(sample, "Total Sales", Expression="SUM ( Sales[LineAmount] ) * 2"),
         {"X-Live-Token": token})
    reader.join(10)
    assert got and got[0]["seq"] == 2 and got[0]["focus"]["name"] == "Total Sales"


def test_token_file_is_private(tmp_path):
    path = tmp_path / "live.json"
    L.write_token_file(8770, "secret", path)
    info = json.loads(path.read_text())
    assert info["token"] == "secret" and info["url"] == "http://127.0.0.1:8770/"
    assert (path.stat().st_mode & 0o077) == 0


def test_the_desktop_button_files():
    folder = ROOT / "dax_lineage" / "live_tool"
    tool = json.loads((folder / "PowerBILive.pbitool.template.json").read_text(encoding="utf-8"))
    assert tool["name"] == "Live lineage" and tool["iconData"].startswith("image/png;base64,")
    watcher = (folder / "PowerBILive.ps1").read_text(encoding="utf-8")
    assert "/api/snapshot" in watcher and "X-Live-Token" in watcher and ".dax_lineage\\live.json" in watcher
    installer = (folder / "install-live.ps1").read_text(encoding="utf-8")
    assert "PowerBILive.ps1" in installer and "%server%" in installer
    assert "msmdsrv.port.txt" in watcher and '[string] $Server = ""' in watcher   # runs without the button too
    launcher = (ROOT / "run-live.bat").read_text(encoding="utf-8")
    assert "dax_lineage\\live_tool\\PowerBILive.ps1" in launcher and "ExecutionPolicy Bypass" in launcher
