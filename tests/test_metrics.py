"""Governance G2: the metric glossary, drift across files, and certified metrics in reviews and changes."""
import threading

import pytest

from dax_lineage.cli import main as daxlin
from dax_lineage.workspace import Store, scan_folders
from dax_lineage.workspace import auth as A
from dax_lineage.workspace import changes as CH
from dax_lineage.workspace import check as C
from dax_lineage.workspace import metrics as M
from dax_lineage.workspace.app import AuthConfig, make_server
from test_review import measures, pbit, save  # noqa: F401  (pbit is a fixture)
from test_workspace import call

PASSWORD = "correct horse battery"


def set_dax(name, expression):
    def edit(schema, layout):
        measures(schema)[name][1]["expression"] = expression
    return edit


def renamed(old, new):
    def edit(schema, layout):
        measures(schema)[old][1]["name"] = new
    return edit


@pytest.fixture
def team(tmp_path, pbit):
    """Three self-contained files with copies of the same measures, one of them drifted."""
    folder = tmp_path / "BI"
    folder.mkdir()
    store = Store(tmp_path / "ws.db")
    store.add_folder(folder)
    save(pbit, folder / "Sales.pbit")
    save(pbit, folder / "Sales copy.pbit", set_dax("Gross Margin", "[Total Sales] - [Total Cost] * 1.02"))
    save(pbit, folder / "Old.pbit", set_dax("Total Sales", "sum(Sales[LineAmount])  // same, written differently"))
    scan_folders(store)
    models = {m["name"]: m for m in store.models()}
    return store, models, folder


def key(store, model, name):
    return next(o for o in store.model(model["id"])["objects"] if o["kind"] == "measure" and o["name"] == name)["object_key"]


def statuses(store, metric_id):
    return {(c["model_name"], c["name"]): c["status"] for c in M.copies(store, M.get(store, metric_id))}


def drift_findings(store, model):
    return [f for f in C.version_check(store, model["version_id"])["findings"] if f["rule_id"] == "metric-drift"]


def test_candidates_before_any_metric(team):
    store, _, _ = team
    found = {c["name"]: c for c in M.candidates(store)}
    assert found["Gross Margin"]["models"] == 3 and found["Gross Margin"]["versions"] == 2
    assert found["Total Sales"]["versions"] == 1                 # formatting and comments do not count
    assert M.candidates(store)[0]["name"] == "Gross Margin"     # different DAX first


def test_draft_certify_and_copies(team):
    store, models, _ = team
    sales = models["Sales"]
    draft = M.create(store, sales["id"], key(store, sales, "Total Sales"), definition="Net invoiced amount", by="Linh")
    assert draft["status"] == "draft" and draft["expression"].startswith("SUM")
    assert drift_findings(store, models["Old"]) == []           # drafts are not enforced
    with pytest.raises(ValueError):
        M.certify(store, draft["id"], by="Minh")                 # no owner yet
    M.update(store, draft["id"], owner="Linh")
    metric = M.certify(store, draft["id"], by="Minh")
    assert metric["status"] == "certified" and metric["certified_by"] == "Minh"
    assert statuses(store, metric["id"]) == {("Sales", "Total Sales"): "defines", ("Old", "Total Sales"): "same",
                                             ("Sales copy", "Total Sales"): "same"}
    assert "Total Sales" not in {c["name"] for c in M.candidates(store)}
    with pytest.raises(ValueError):
        M.create(store, models["Old"]["id"], key(store, models["Old"], "Total Sales"))   # one metric per name
    with pytest.raises(ValueError):
        M.delete(store, metric["id"])                             # certified metrics are deprecated, not deleted


def test_drift_is_a_finding_until_accepted(team):
    store, models, _ = team
    sales, copy = models["Sales"], models["Sales copy"]
    metric = M.create(store, sales["id"], key(store, sales, "Gross Margin"), owner="Linh")
    before = store.models()
    M.certify(store, metric["id"], by="Minh")
    assert statuses(store, metric["id"])[("Sales copy", "Gross Margin")] == "drift"
    found = drift_findings(store, copy)
    assert [f["label"] for f in found] == ["Gross Margin"] and "owner Linh" in found[0]["message"]
    score = lambda name: next(m["score"] for m in store.models() if m["name"] == name)
    assert score("Sales copy") < next(m["score"] for m in before if m["name"] == "Sales copy")
    with pytest.raises(ValueError):
        M.accept_variant(store, metric["id"], copy["id"], key(store, copy, "Gross Margin"), " ")
    M.accept_variant(store, metric["id"], copy["id"], key(store, copy, "Gross Margin"), "includes 2% freight", "Minh")
    assert statuses(store, metric["id"])[("Sales copy", "Gross Margin")] == "variant"
    assert drift_findings(store, copy) == []
    assert M.withdraw_variant(store, metric["id"], copy["id"], key(store, copy, "Gross Margin"))
    assert len(drift_findings(store, copy)) == 1


def test_source_changes_and_recertify(team, pbit):
    store, models, folder = team
    sales = models["Sales"]
    metric = M.certify(store, M.create(store, sales["id"], key(store, sales, "Gross Margin"), owner="Linh")["id"])
    save(pbit, folder / "Sales.pbit", set_dax("Gross Margin", "[Total Sales] - [Total Cost] * 1.02"), bump=60)
    scan_folders(store)
    assert M.source_state(store, M.get(store, metric["id"]))["state"] == "changed"
    sales = next(m for m in store.models() if m["name"] == "Sales")
    assert "re-certifies it" in drift_findings(store, sales)[0]["message"]
    assert statuses(store, metric["id"])[("Sales copy", "Gross Margin")] == "drift"   # still against the agreed DAX
    M.certify(store, metric["id"], by="Linh")                                         # the new DAX is agreed now
    assert drift_findings(store, sales) == []
    assert statuses(store, metric["id"])[("Sales copy", "Gross Margin")] == "same"
    assert statuses(store, metric["id"])[("Old", "Gross Margin")] == "drift"


def test_rename_keeps_the_link_and_aliases_match(team, pbit):
    store, models, folder = team
    sales = models["Sales"]
    metric = M.create(store, sales["id"], key(store, sales, "Total Sales"), owner="Linh", aliases="Net Revenue")
    M.certify(store, metric["id"])
    save(pbit, folder / "Old.pbit", renamed("Total Sales", "Net Revenue"), bump=60)
    scan_folders(store)
    assert statuses(store, metric["id"])[("Old", "Net Revenue")] == "same"
    old = next(m for m in store.models() if m["name"] == "Old")
    assert M.for_object(store, old["id"], key(store, old, "Net Revenue"), "Net Revenue")["copy"] == "same"
    with pytest.raises(ValueError):
        M.update(store, M.create(store, sales["id"], key(store, sales, "Total Cost"))["id"], aliases=["net revenue"])


def test_deprecate(team):
    store, models, _ = team
    sales = models["Sales"]
    metric = M.certify(store, M.create(store, sales["id"], key(store, sales, "Order Count"), owner="Linh")["id"])
    M.deprecate(store, metric["id"], by="Linh", note="use Orders instead")
    assert M.get(store, metric["id"])["status"] == "deprecated"
    found = drift_findings(store, models["Old"])
    assert found and "deprecated metric" in found[0]["message"]


def test_changes_to_a_certified_metric(team):
    store, models, _ = team
    sales = models["Sales"]
    gm = key(store, sales, "Gross Margin")
    M.certify(store, M.create(store, sales["id"], gm, owner="Minh")["id"])
    planned = CH.plan(store, sales["id"], [{"op": "remove", "object_key": gm}])
    assert planned["problems"] and "deprecate it" in planned["problems"][0]
    planned = CH.plan(store, sales["id"], [{"op": "set", "object_key": gm, "field": "description", "value": "GM"}])
    assert not planned["problems"] and any("Minh" in w for w in planned["warnings"])


def test_api_roles_and_owner_approval(team):
    store, models, _ = team
    sales = models["Sales"]
    for name, role, display in (("aut", "author", "Aut"), ("rev", "reviewer", "Rev"), ("minh", "reviewer", "Minh"),
                                ("ada", "admin", "Ada")):
        A.add_account(store, name, role, display, PASSWORD)
    httpd = make_server(store, port=0, quiet=True, auth=AuthConfig(mode="local"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def sign_in(name):
        _, _, response = call(port, "POST", "/api/login", {"username": name, "password": PASSWORD})
        return {"Cookie": response.getheader("Set-Cookie").split(";")[0]}

    aut, rev, minh, ada = (sign_in(n) for n in ("aut", "rev", "minh", "ada"))
    try:
        gm = key(store, sales, "Gross Margin")
        status, made, _ = call(port, "POST", "/api/metrics", {"model_id": sales["id"], "object_key": gm,
                                                               "owner": "Minh", "definition": "Sales minus cost"}, aut)
        assert status == 200 and made["status"] == "draft" and made["created_by"] == "Aut"
        path = f"/api/metrics/{made['id']}"
        assert call(port, "POST", path, {"action": "certify"}, aut)[0] == 403
        assert call(port, "POST", path, {"action": "certify"}, rev)[1]["certified_by"] == "Rev"
        assert call(port, "POST", path, {"action": "update", "definition": "x"}, aut)[0] == 403
        assert call(port, "POST", path, {"action": "explode"}, rev)[0] == 400
        status, glossary, _ = call(port, "GET", "/api/metrics", None, aut)
        assert status == 200 and glossary["metrics"][0]["counts"]["drift"] == 1
        status, detail, _ = call(port, "GET", path, None, aut)
        assert {c["status"] for c in detail["copies"]} == {"defines", "same", "drift"}
        copy = next(c for c in detail["copies"] if c["status"] == "drift")
        variant = {"model_id": copy["model_id"], "object_key": copy["object_key"], "reason": "freight"}
        assert call(port, "POST", f"{path}/variants", variant, aut)[0] == 403
        assert call(port, "POST", f"{path}/variants", variant, rev)[0] == 200
        obj = call(port, "GET", f"/api/objects/{copy['id']}", None, aut)[1]
        assert obj["metric"]["copy"] == "variant" and obj["metric"]["name"] == "Gross Margin"

        # a change to the measure that defines it: its owner or an admin approves, not any reviewer
        status, change, _ = call(port, "POST", "/api/changes", {"model_id": sales["id"], "items": [
            {"op": "set", "object_key": gm, "field": "description", "value": "Sales minus cost"}]}, aut)
        decision = f"/api/changes/{change['id']}/decision"
        status, error, _ = call(port, "POST", decision, {"status": "approved"}, rev)
        assert status == 403 and "owner (Minh)" in error["error"]
        assert call(port, "POST", decision, {"status": "approved"}, minh)[0] == 200
        status, change2, _ = call(port, "POST", "/api/changes", {"model_id": sales["id"], "items": [
            {"op": "set", "object_key": gm, "field": "display_folder", "value": "Margins"}]}, aut)
        assert call(port, "POST", f"/api/changes/{change2['id']}/decision", {"status": "approved"}, ada)[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_review_flags_a_certified_metric(team, pbit):
    store, models, folder = team
    sales = models["Sales"]
    M.certify(store, M.create(store, sales["id"], key(store, sales, "Gross Margin"), owner="Minh")["id"])
    save(pbit, folder / "Sales.pbit", set_dax("Gross Margin", "[Total Sales] - [Total Cost] - 1"), bump=60)
    scan_folders(store)
    httpd = make_server(store, port=0, quiet=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        version = next(m for m in store.models() if m["name"] == "Sales")["version_id"]
        review = call(httpd.server_address[1], "GET", f"/api/versions/{version}")[1]
        assert [m["name"] for m in review["metrics"]] == ["Gross Margin"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cli(team, capsys):
    store, models, _ = team
    sales = models["Sales"]
    M.certify(store, M.create(store, sales["id"], key(store, sales, "Gross Margin"), owner="Minh")["id"])
    db = ["workspace", "--db", str(store.path)]
    daxlin(db + ["metrics"])
    out = capsys.readouterr().out
    assert "Gross Margin" in out and "certified" in out
    daxlin(db + ["metrics", "drift"])
    assert "Sales copy" in capsys.readouterr().out
    daxlin(db + ["metrics", "candidates"])
    assert "Total Sales" in capsys.readouterr().out
