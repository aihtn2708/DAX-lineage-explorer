"""Team workspace, milestone 1: scanning, versions, notes that follow objects, search, and the web API."""
import http.client
import importlib.util
import json
import os
import shutil
import threading
import zipfile
from pathlib import Path

import pytest

from dax_lineage.cli import main as daxlin
from dax_lineage.pbi_report import decode
from dax_lineage.workspace import Store, scan_folders
from dax_lineage.workspace import scan as scan_module
from dax_lineage.workspace.app import make_server

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def reports(tmp_path_factory):
    spec = importlib.util.spec_from_file_location("make_report_samples", ROOT / "scripts" / "make_report_samples.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path_factory.mktemp("reports")
    module.write_fixtures(target)
    return target


def tagged_pbit(source: Path, target: Path, rename=None, expression=None) -> Path:
    """A copy of a .pbit whose measures carry lineage tags, optionally with one measure renamed or edited."""
    with zipfile.ZipFile(source) as z:
        files = {n: z.read(n) for n in z.namelist()}
    schema = json.loads(decode(files["DataModelSchema"]))
    for table in schema["model"]["tables"]:
        for m in table.get("measures", []):
            m["lineageTag"] = "tag-" + m["name"].lower().replace(" ", "-")
            if rename and m["name"] == rename[0]:
                m["name"] = rename[1]
            if expression and m["name"] == expression[0]:
                m["expression"] = expression[1]
    files["DataModelSchema"] = json.dumps(schema).encode("utf-16-le")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in files.items():
            z.writestr(name, body)
    return target


def bump(path: Path, seconds: int = 60):
    st = path.stat()
    os.utime(path, (st.st_atime + seconds, st.st_mtime + seconds))


@pytest.fixture
def team(tmp_path, reports):
    folder = tmp_path / "BI - Reports"
    folder.mkdir()
    tagged_pbit(reports / "sales.pbit", folder / "Sales.pbit")
    shutil.copy(reports / "sales.pbix", folder / "Sales report.pbix")
    store = Store(tmp_path / "ws.db")
    store.add_folder(folder)
    return store, folder


def measure(store, model_name, name):
    model = next(m for m in store.models() if m["name"] == model_name)
    found = [o for o in store.model(model["id"])["objects"] if o["kind"] == "measure" and o["name"] == name]
    return model, (found[0] if found else None)


def test_scan_records_each_file_as_a_model(team):
    store, _ = team
    summary = scan_folders(store)
    assert (summary["files"], summary["new_versions"], summary["failed"]) == (2, 2, 0)
    models = {m["name"]: m for m in store.models()}
    sales = models["Sales"]
    assert sales["status"] == "ok" and sales["kind"] == "model" and sales["version"] == 1
    assert sales["measures"] > 0 and sales["visuals"] > 0 and sales["has_report"]
    assert sales["unused_measures"] == len([o for o in store.model(sales["id"])["objects"]
                                            if o["kind"] == "measure" and o["usage_status"] in ("unused", "deadChain")])
    # the .pbix fixture's model is a stand-in blob: recorded, with its report, and flagged
    pbix = models["Sales report"]
    assert pbix["status"] == "partial" and "could not be read" in pbix["note"] and pbix["visuals"] > 0


def test_object_keys_are_lineage_tags(team):
    store, _ = team
    scan_folders(store)
    _, total = measure(store, "Sales", "Total Sales")
    assert store.object(total["id"])["object_key"] == "tag-total-sales"


def test_unchanged_files_are_not_read_again(team, monkeypatch):
    store, _ = team
    scan_folders(store)
    monkeypatch.setattr(scan_module, "analyse", lambda path: pytest.fail(f"read {path} again"))
    summary = scan_folders(store)
    assert (summary["unchanged"], summary["new_versions"]) == (2, 0)
    # touched but identical: hashed, still not read
    for f in team[1].iterdir():
        bump(f)
    assert scan_folders(store)["unchanged"] == 2
    assert all(m["versions"] == 1 for m in store.models())


def test_notes_follow_a_measure_through_a_rename(team, reports):
    store, folder = team
    scan_folders(store)
    model, total = measure(store, "Sales", "Total Sales")
    key = store.object(total["id"])["object_key"]
    store.annotate(model["id"], key, owner="Linh", note="the headline number", decision="keep", by="An")

    tagged_pbit(reports / "sales.pbit", folder / "Sales.pbit", rename=("Total Sales", "Net Sales"),
                expression=("Net Sales", "SUM(Sales[Amount]) - 0"))
    bump(folder / "Sales.pbit")
    summary = scan_folders(store)
    assert (summary["new_versions"], summary["unchanged"]) == (1, 1)

    model, renamed = measure(store, "Sales", "Net Sales")
    assert model["version"] == 2
    # measures still reading [Total Sales] now point at nothing: what a rename by hand breaks
    old = measure(store, "Sales", "Total Sales")[1]
    assert old["in_model"] == 0 and old["read_by"] > 0
    assert (renamed["owner"], renamed["decision"]) == ("Linh", "keep")
    detail = store.object(renamed["id"])
    assert detail["annotation"]["note"] == "the headline number"
    assert [(h["version"], h["name"]) for h in detail["history"]] == [(2, "Net Sales"), (1, "Total Sales")]
    assert detail["history"][0]["expression"] != detail["history"][1]["expression"]


def test_missing_lock_and_broken_files(team):
    store, folder = team
    scan_folders(store)
    (folder / "~$Sales.pbix").write_bytes(b"lock")
    (folder / "Broken.pbix").write_bytes(b"not a zip")
    (folder / "Sales report.pbix").unlink()
    summary = scan_folders(store)
    assert summary["files"] == 2 and summary["failed"] == 1 and summary["missing"] == 1
    models = {m["name"]: m for m in store.models()}
    assert "~$Sales" not in models
    assert models["Broken"]["status"] == "error" and "not a zip" in models["Broken"]["note"]
    assert models["Sales report"]["missing"] == 1
    # an unchanged broken file is not retried every night
    assert scan_folders(store)["new_versions"] == 0


def test_search_across_models_and_elsewhere(team, reports):
    store, folder = team
    tagged_pbit(reports / "sales.pbit", folder / "Sales copy.pbit")
    scan_folders(store)
    hits = [h for h in store.search("total sales") if h["kind"] == "measure" and h["name"] == "Total Sales"]
    assert {h["model_name"] for h in hits if h["in_model"]} == {"Sales", "Sales copy"}
    # the .pbix report's visuals use it too, though its model was not read
    assert [h["model_name"] for h in hits if not h["in_model"]] == ["Sales report"]
    detail = store.object(hits[0]["id"])
    assert {e["model_name"] for e in detail["elsewhere"]} >= {"Sales copy" if hits[0]["model_name"] == "Sales" else "Sales"}
    in_dax = store.search("SUM (", in_dax=True)
    assert in_dax and all(h["dax_match"] for h in in_dax)
    assert store.search("%") == [] or all("%" in h["name"] for h in store.search("%"))


def test_annotation_rules(team):
    store, _ = team
    scan_folders(store)
    model, total = measure(store, "Sales", "Total Sales")
    with pytest.raises(ValueError):
        store.annotate(model["id"], "x", decision="delete")
    with pytest.raises(KeyError):
        store.annotate(999, "x", note="n")
    store.annotate(model["id"], total["object_key"], decision="remove", by="An")
    assert [d["name"] for d in store.decisions("remove")] == ["Total Sales"]
    store.annotate(model["id"], total["object_key"])  # all empty: removed
    assert store.decisions("remove") == []


# -- the web API ----------------------------------------------------------------

@pytest.fixture
def server(team):
    store, _ = team
    scan_folders(store)
    httpd = make_server(store, port=0, quiet=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield store, httpd.server_address[1], httpd
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None, host=None):
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    if data is not None and "Content-Type" not in hdrs:
        hdrs["Content-Type"] = "application/json"
    con.request(method, path, body=data, headers=hdrs)
    response = con.getresponse()
    raw = response.read()
    con.close()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = raw
    return response.status, payload, response


def test_api_reads(server):
    store, port, _ = server
    status, page, response = call(port, "GET", "/")
    assert status == 200 and b"/app.js" in page and "script-src 'self'" in response.getheader("Content-Security-Policy")
    assert call(port, "GET", "/app.js")[0] == 200
    status, models, _ = call(port, "GET", "/api/models")
    sales = next(m for m in models if m["name"] == "Sales")
    status, detail, _ = call(port, "GET", f"/api/models/{sales['id']}")
    assert status == 200 and detail["objects"] and detail["history"]
    hit = call(port, "GET", "/api/search?q=total%20sales")[1][0]
    status, obj, _ = call(port, "GET", f"/api/objects/{hit['id']}")
    assert status == 200 and obj["name"] == "Total Sales" and "visuals" in obj and "elsewhere" in obj
    assert call(port, "GET", "/api/objects/99999")[0] == 404
    assert call(port, "GET", "/api/nothing")[0] == 404
    assert call(port, "GET", "/../pyproject.toml")[0] == 404
    assert call(port, "GET", "/api/summary")[1]["models"] == 2


def test_api_writes_and_guards(server):
    store, port, _ = server
    hit = call(port, "GET", "/api/search?q=total%20sales")[1][0]
    obj = call(port, "GET", f"/api/objects/{hit['id']}")[1]
    note = {"model_id": obj["model_id"], "object_key": obj["object_key"], "decision": "remove", "note": "<b>x</b>", "by": "An"}

    assert call(port, "POST", "/api/annotations", note, host="attacker.example")[0] == 421
    assert call(port, "POST", "/api/annotations", note, {"Origin": "http://attacker.example"})[0] == 403
    assert call(port, "POST", "/api/annotations", b"model_id=1", {"Content-Type": "application/x-www-form-urlencoded"})[0] == 415
    assert call(port, "POST", "/api/annotations", b"{" + b" " * 70000 + b"}")[0] == 413
    assert call(port, "POST", "/api/annotations", b"[1]")[0] == 400
    assert call(port, "OPTIONS", "/api/annotations")[0] == 405
    assert call(port, "POST", "/api/annotations", dict(note, decision="burn"))[0] == 400
    assert store.decisions("remove") == []

    status, saved, _ = call(port, "POST", "/api/annotations", note, {"Origin": f"http://127.0.0.1:{port}"})
    assert status == 200 and saved["decision"] == "remove"
    assert store.object(hit["id"])["annotation"]["note"] == "<b>x</b>"  # stored as text, shown as text
    assert call(port, "POST", f"/api/models/{obj['model_id']}", {"owner": "Linh", "team": "Finance BI"})[0] == 200
    assert store.model(obj["model_id"])["owner"] == "Linh"


def test_scan_from_the_page(server):
    store, port, _ = server
    status, state, _ = call(port, "POST", "/api/scan", {})
    assert status in (202, 409)
    for _ in range(100):
        state = call(port, "GET", "/api/scan")[1]
        if not state["running"]:
            break
        threading.Event().wait(0.05)
    assert not state["running"] and any("unchanged" in line for line in state["log"])


def test_read_only(team):
    store, _ = team
    httpd = make_server(store, port=0, quiet=True, read_only=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        assert call(port, "POST", "/api/scan", {})[0] == 403
        assert call(port, "GET", "/api/summary")[1]["read_only"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cli(team, capsys):
    store, folder = team
    db = ["workspace", "--db", str(store.path)]
    assert daxlin(db + ["scan"]) == 0
    assert daxlin(db + ["models"]) == 0
    out = capsys.readouterr().out
    assert "2 new versions" in out and "Sales" in out
    daxlin(db + ["search", "Total Sales"])
    assert "Total Sales" in capsys.readouterr().out
    assert daxlin(db + ["folders"]) == 0 and str(folder.resolve()) in capsys.readouterr().out
    with pytest.raises(SystemExit):
        daxlin(db + ["add-folder", str(folder / "nope")])


def test_real_pbix_when_given(tmp_path):
    path = os.environ.get("DAX_LINEAGE_PBIX")
    if not path:
        pytest.skip("set DAX_LINEAGE_PBIX to check a real file")
    shutil.copy(path, tmp_path / "Real.pbix")
    store = Store(tmp_path / "ws.db")
    summary = scan_folders(store, [tmp_path])
    assert summary["failed"] == 0 and store.models()[0]["status"] == "ok"
