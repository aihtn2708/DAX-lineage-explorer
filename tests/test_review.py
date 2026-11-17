"""Team workspace, milestone 2: what changed between two saved versions, and reviewing it."""
import importlib.util
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest

from dax_lineage.cli import main as daxlin
from dax_lineage.pbi_report import decode
from dax_lineage.workspace import Store, scan_folders
from dax_lineage.workspace.app import make_server
from dax_lineage.workspace.diff import as_text, compare, dax_diff
from test_workspace import call

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def pbit(tmp_path_factory):
    spec = importlib.util.spec_from_file_location("make_report_samples", ROOT / "scripts" / "make_report_samples.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path_factory.mktemp("reports")
    module.write_fixtures(target)
    return target / "sales.pbit"


def measures(schema):
    return {m["name"]: (t, m) for t in schema["model"]["tables"] for m in t.get("measures", [])}


def save(source: Path, target: Path, edit=None, tags=True, bump=0):
    """Write ``target`` from the fixture .pbit, tagged, after ``edit(schema, layout)`` - like a save in Desktop."""
    with zipfile.ZipFile(source) as z:
        files = {n: z.read(n) for n in z.namelist()}
    schema, layout = json.loads(decode(files["DataModelSchema"])), json.loads(decode(files["Report/Layout"]))
    for _, m in measures(schema).values():
        if tags:
            m["lineageTag"] = "tag-" + m["name"].lower().replace(" ", "-")
    if edit:
        edit(schema, layout)
    files["DataModelSchema"] = json.dumps(schema).encode("utf-16-le")
    files["Report/Layout"] = json.dumps(layout).encode("utf-16-le")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in files.items():
            z.writestr(name, body)
    if bump:
        st = target.stat()
        os.utime(target, (st.st_atime + bump, st.st_mtime + bump))
    return target


def rename(schema, layout, old, new):
    """What Desktop does on a rename: the measure, every DAX reference, every visual."""
    for t, m in measures(schema).values():
        if m["name"] == old:
            m["name"] = new
        text = "\n".join(m["expression"]) if isinstance(m["expression"], list) else m["expression"]
        m["expression"] = text.replace(f"[{old}]", f"[{new}]")
    for section in layout["sections"]:
        for vc in section["visualContainers"]:
            for part in ("config", "filters"):
                if isinstance(vc.get(part), str):
                    vc[part] = vc[part].replace(f'{old}"', f'{new}"')


def the_save(schema, layout):
    rename(schema, layout, "Total Sales", "Net Sales")
    found = measures(schema)
    found["Sales PY"][1]["expression"] = "CALCULATE (\n    [Net Sales],\n    DATEADD ( 'Date'[Date], -1, YEAR )\n)"
    found["Total Quantity"][1]["expression"] = "SUM(Sales[Quantity])"                    # formatting only
    found["Order Count"][1]["isHidden"] = True
    found["Order Count"][1]["formatString"] = "0.0"
    table, gm = found["Gross Margin %"]
    table["measures"].remove(gm)                                                         # still used: breaks
    found["Total Cost"][0]["measures"].append({"name": "Sales Target", "expression": "[Net Sales] * 1.1",
                                               "lineageTag": "tag-sales-target"})
    for section in layout["sections"]:
        for vc in list(section["visualContainers"]):
            config = json.loads(vc["config"])
            if config["name"] == "slicer1":
                section["visualContainers"].remove(vc)
            if config["name"] == "line2":
                vc["config"] = vc["config"].replace('Order Count"', 'Sales YTD"')


@pytest.fixture
def reviewed(tmp_path, pbit):
    folder = tmp_path / "BI"
    folder.mkdir()
    store = Store(tmp_path / "ws.db")
    store.add_folder(folder)
    save(pbit, folder / "Sales.pbit")
    scan_folders(store)
    save(pbit, folder / "Sales.pbit", the_save, bump=60)
    scan_folders(store)
    model = store.models()[0]
    return store, model, compare(store, model["version_id"]), folder


def test_first_version_is_the_baseline(tmp_path, pbit):
    store = Store(tmp_path / "ws.db")
    save(pbit, tmp_path / "Sales.pbit")
    scan_folders(store, [tmp_path])
    v = store.models()[0]
    review = compare(store, v["version_id"])
    assert review["baseline"] and store.version(v["version_id"])["review"] == "baseline"
    assert store.reviews("pending") == []
    with pytest.raises(ValueError):
        store.set_review(v["version_id"], "approved", by="An")


def test_rename_is_one_change_not_a_removal_and_an_addition(reviewed):
    _, _, review, _ = reviewed
    assert [(r["old_label"], r["label"], r["guessed"]) for r in review["renamed"]] == [("Total Sales", "Net Sales", False)]
    labels = lambda items: [i["label"] for i in items]
    assert "Net Sales" not in labels(review["added"]) and "Total Sales" not in labels(review["removed"])
    # the visuals that show it were rewritten by the rename, so they are not reported as changed
    assert not [v for v in review["visuals"]["changed"] if "Net Sales" in v["fields_added"]]


def test_edits_and_dax_diff(reviewed):
    _, _, review, _ = reviewed
    edited = {e["label"]: e for e in review["edited"]}
    py = edited["Sales PY"]
    assert py["changes"] == ["DAX"] and not py["dax"]["formatting_only"] and py["impact"] > 0
    assert {"op": "-", "text": "    SAMEPERIODLASTYEAR ( 'Date'[Date] )"} in py["dax"]["lines"]
    assert {"op": "+", "text": "    DATEADD ( 'Date'[Date], -1, YEAR )"} in py["dax"]["lines"]
    assert py["reads_added"] == [] and py["reads_removed"] == []  # [Net Sales] is the renamed [Total Sales]
    assert edited["Total Quantity"]["dax"]["formatting_only"]
    assert set(edited["Order Count"]["changes"]) == {"format string", "hidden"}
    # measures whose DAX only followed the rename are kept, but marked, and not counted as DAX changes
    assert edited["Gross Margin"]["follows_rename"] and edited["Gross Margin"]["dax"]["lines"][0]["op"] == "-"
    assert not py.get("follows_rename")
    c = review["counts"]
    assert (c["dax"], c["formatting"], c["follow_renames"]) == (1, 1, 13)


def test_added_removed_and_what_broke(reviewed):
    _, _, review, _ = reviewed
    assert [a["label"] for a in review["added"]] == ["Sales Target"]
    removed = {r["label"]: r for r in review["removed"]}
    assert set(removed) == {"Gross Margin %"} and removed["Gross Margin %"]["impact"] > 0
    broken = {b["label"]: b["used_by"] for b in review["broken"]}
    assert "Avg Category Margin %" in broken["Gross Margin %"]
    assert any(u.startswith("visual") for u in broken["Gross Margin %"])
    became = {u["label"]: u for u in review["usage"]}
    assert became["Sales YTD"]["became_unused"] is False  # line2 shows it now
    assert "Order Count" not in became                     # still on the customer table


def test_visual_changes(reviewed):
    _, _, review, _ = reviewed
    v = review["visuals"]
    assert [x["title"] for x in v["removed"]] == ["slicer1"] or [x["type"] for x in v["removed"]] == ["slicer"]
    assert len(v["changed"]) == 1  # a rename or a removed measure does not make its visuals "changed"
    line2 = v["changed"][0]
    assert line2["fields_added"] == ["Sales YTD"] and line2["fields_removed"] == ["Order Count"]


def test_untagged_models_pair_probable_renames(tmp_path, pbit):
    store = Store(tmp_path / "ws.db")
    save(pbit, tmp_path / "Sales.pbit", tags=False)
    scan_folders(store, [tmp_path])
    save(pbit, tmp_path / "Sales.pbit", lambda s, l: rename(s, l, "Units per Order", "Units / Order"), tags=False, bump=60)
    scan_folders(store, [tmp_path])
    review = compare(store, store.models()[0]["version_id"])
    assert [(r["old_label"], r["label"], r["guessed"]) for r in review["renamed"]] == [("Units per Order", "Units / Order", True)]
    assert not review["added"] and not review["removed"]


def test_no_change_and_compare_any_two(reviewed, pbit):
    store, model, _, folder = reviewed
    save(pbit, folder / "Sales.pbit", the_save, bump=120)          # same content, saved again: no new version
    assert scan_folders(store)["new_versions"] == 0
    versions = store.versions_of(model["id"])
    back = compare(store, versions[-1]["id"], versions[0]["id"])     # v1 against v2: everything reversed
    assert [r["label"] for r in back["renamed"]] == ["Total Sales"] and [a["label"] for a in back["added"]] == ["Gross Margin %"]
    with pytest.raises(KeyError):
        compare(store, 999999)


def test_text_summary(reviewed):
    _, _, review, _ = reviewed
    text = as_text(review)
    assert "renamed Total Sales -> Net Sales" in text and "+ measure Sales Target" in text
    assert "- measure Gross Margin %" in text and "broken: Gross Margin %" in text
    assert "DAX formatting" in text and "+     DATEADD ( 'Date'[Date], -1, YEAR )" in text
    assert "13 expressions only follow the rename" in text and "Gross Margin: DAX" not in text


def test_review_flow(reviewed):
    store, model, _, _ = reviewed
    vid = model["version_id"]
    assert [r["id"] for r in store.reviews("pending")] == [vid] and store.summary()["pending_reviews"] == 1
    store.add_comment(vid, "Why DATEADD here?", by="Minh", object_key="tag-sales-py")
    with pytest.raises(ValueError):
        store.add_comment(vid, "   ", by="Minh")
    store.set_review(vid, "changes_requested", by="Minh", note="Gross Margin % is still on the Overview card")
    assert store.version(vid)["review"] == "changes_requested" and store.reviews("pending") == []
    store.set_review(vid, "approved", by="Linh")
    v = store.version(vid)
    assert (v["review"], v["reviewer"]) == ("approved", "Linh")
    kinds = [c["kind"] for c in store.comments(vid)]
    assert kinds == ["comment", "changes_requested", "approved"]
    with pytest.raises(ValueError):
        store.set_review(vid, "merged")


def test_review_api(reviewed):
    store, model, _, _ = reviewed
    httpd = make_server(store, port=0, quiet=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port, vid = httpd.server_address[1], model["version_id"]
    try:
        status, queue, _ = call(port, "GET", "/api/reviews")
        assert status == 200 and [q["id"] for q in queue] == [vid] and queue[0]["changes"]["total"] > 0
        status, review, _ = call(port, "GET", f"/api/versions/{vid}")
        assert status == 200 and review["renamed"] and review["comments"] == [] and review["versions"]
        assert call(port, "GET", f"/api/versions/{vid}?against=999999")[0] == 404
        assert call(port, "GET", f"/api/versions/{vid}?against=x")[0] == 400
        assert call(port, "GET", "/api/versions/999999")[0] == 404
        assert call(port, "POST", f"/api/versions/{vid}/comments", {"body": "Looks fine", "by": "An"})[0] == 200
        assert call(port, "POST", f"/api/versions/{vid}/review", {"status": "approved", "by": "An"},
                    {"Origin": "http://attacker.example"})[0] == 403
        assert call(port, "POST", f"/api/versions/{vid}/review", {"status": "approved", "by": "An"})[0] == 200
        assert call(port, "GET", "/api/reviews")[1] == []
        assert len(call(port, "GET", "/api/reviews?status=all")[1]) == 1
        assert call(port, "GET", f"/api/versions/{vid}")[1]["version"]["review"] == "approved"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cli_changes_and_review(reviewed, capsys):
    store, _, _, _ = reviewed
    db = ["workspace", "--db", str(store.path)]
    assert daxlin(db + ["review"]) == 0
    assert "Sales" in capsys.readouterr().out
    assert daxlin(db + ["changes", "Sales"]) == 0
    out = capsys.readouterr().out
    assert "renamed Total Sales -> Net Sales" in out
    assert daxlin(db + ["changes", "--since", "1d"]) == 0
    assert "Sales v2" in capsys.readouterr().out
    assert daxlin(db + ["changes", "Sales", "--version", "1"]) == 0
    assert "first version" in capsys.readouterr().out


def test_dax_diff_small_cases():
    assert dax_diff("a", "a")["lines"] == [{"op": " ", "text": "a"}]
    assert dax_diff("SUM ( x[a] )", "sum(x[a])")["formatting_only"]
    assert dax_diff("SUM ( x[a] ) // total", "SUM ( x[a] )")["formatting_only"]
    assert not dax_diff("SUM ( x[a] )", "SUM ( x[b] )")["formatting_only"]
    assert not dax_diff('"Total"', '"total"')["formatting_only"]  # text inside strings is meaning
