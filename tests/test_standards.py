"""Team workspace, milestone 3: rules, scores, exceptions, and the standards in reviews."""
import json
import shutil
import threading
from pathlib import Path

import pytest

from dax_lineage.cli import main as daxlin
from dax_lineage.workspace import Store, scan_folders
from dax_lineage.workspace import check as C
from dax_lineage.workspace.rules import RULES, score
from test_review import measures, pbit, save  # noqa: F401  (pbit is a fixture)
from test_workspace import call


def add(table_name, name, expression, hidden=False):
    def edit(schema, layout):
        table = next(t for t in schema["model"]["tables"] if t["name"] == table_name)
        table.setdefault("measures", []).append({"name": name, "expression": expression, "isHidden": hidden,
                                                 "lineageTag": "tag-" + name.lower().replace(" ", "-")})
    return edit


def edits(*fns):
    def edit(schema, layout):
        for fn in fns:
            fn(schema, layout)
    return edit


TROUBLE = edits(
    add("Measures", "Bad Ratio", "[Total Sales] / [Order Count]"),
    add("Measures", "Safe Ratio", "IFERROR ( [Total Sales] / [Order Count], 0 )"),
    add("Measures", "Copy of Total", "sum(Sales[LineAmount])"),
    add("Measures", "Double  Space", "1"),
    add("Measures", "_Visible Helper", "2"),
    add("Measures", "Reads Nothing", "[Gone Measure] + 1"),
)


@pytest.fixture
def ws(tmp_path, pbit):
    folder = tmp_path / "BI"
    folder.mkdir()
    store = Store(tmp_path / "ws.db")
    store.add_folder(folder)
    save(pbit, folder / "Sales.pbit", TROUBLE)
    scan_folders(store)
    model = store.models()[0]
    return store, model, folder


def findings(store, model, rule_id=None):
    result = C.version_check(store, model["version_id"])
    return [f for f in result["findings"] if rule_id is None or f["rule_id"] == rule_id]


def labels(store, model, rule_id, open_only=True):
    return sorted(f["label"] for f in findings(store, model, rule_id) if not (open_only and f["accepted"]))


def test_every_rule_has_a_title_and_a_valid_default():
    ids = [r.id for r in RULES]
    assert len(ids) == len(set(ids))
    for r in RULES:
        assert r.title and r.why and r.severity in ("error", "warning", "info")


def test_rules_find_what_they_should(ws):
    store, model, _ = ws
    assert labels(store, model, "use-divide") == ["Bad Ratio", "Safe Ratio"]  # a / inside IFERROR still counts
    assert "Safe Ratio" in labels(store, model, "avoid-iferror")
    assert labels(store, model, "duplicate-dax") == ["Copy of Total", "Total Sales"]   # case and spaces do not hide it
    assert labels(store, model, "measure-naming") == ["Double  Space"]
    assert labels(store, model, "helper-hidden") == ["_Visible Helper"]
    assert labels(store, model, "broken-reference") == ["Gone Measure"]
    assert "Bad Ratio" in labels(store, model, "unused-measure")
    assert "Bad Ratio" in labels(store, model, "measure-description")
    assert "Total Sales" not in labels(store, model, "measure-description")  # it has one
    assert labels(store, model, "column-description") == []                 # off by default
    assert labels(store, model, "measure-depth") == []


def test_the_score_is_stored_with_each_scan(ws):
    store, model, _ = ws
    m = store.models()[0]
    assert 0 < m["score"] < 100 and m["rule_errors"] == 1
    scan = store.last_run()
    assert "standards score" in scan["log"]


def test_scoring_weights_and_exceptions():
    rule = lambda sev: {"id": sev, "severity": sev}
    obj = lambda key: ({"object_key": key}, "x")
    results = [(rule("error"), 1, [obj("a")]), (rule("info"), 10, [obj("b")])]
    assert score(results, set())["score"] == round(100 * (3 * 0 + 1 * 0.9) / 4)
    accepted = score(results, {("error", "a")})
    assert accepted["score"] == round(100 * (3 + 0.9) / 4) and accepted["accepted"] == 1 and accepted["error"] == 0
    assert score([], set())["score"] == 100


def test_changing_a_rule_rechecks(ws):
    store, model, _ = ws
    before = store.models()[0]["score"]
    C.set_rule(store, "measure-description", enabled=False, by="An")
    assert labels(store, model, "measure-description") == [] and store.models()[0]["score"] > before
    C.set_rule(store, "measure-depth", enabled=True, params={"max_depth": 1})
    assert labels(store, model, "measure-depth")
    C.set_rule(store, "measure-naming", params={"pattern": "^[A-Z_]"})
    assert "Double  Space" in labels(store, model, "measure-naming")
    assert "% on" not in " ".join(labels(store, model, "measure-naming"))
    C.set_rule(store, "broken-reference", severity="warning")
    assert store.models()[0]["rule_errors"] == 0
    with pytest.raises(ValueError):
        C.set_rule(store, "measure-naming", params={"pattern": "("})
    with pytest.raises(ValueError):
        C.set_rule(store, "measure-depth", params={"max_depth": "deep"})
    with pytest.raises(ValueError):
        C.set_rule(store, "measure-depth", params={"colour": "red"})
    with pytest.raises(ValueError):
        C.set_rule(store, "measure-depth", severity="fatal")
    with pytest.raises(KeyError):
        C.set_rule(store, "nope", enabled=True)
    C.reset_rules(store)
    assert store.models()[0]["score"] == before


def test_exceptions(ws):
    store, model, _ = ws
    before = store.models()[0]["score"]
    with pytest.raises(ValueError):
        C.accept(store, model["id"], "tag-bad-ratio", "use-divide", "  ")
    C.accept(store, model["id"], "tag-bad-ratio", "use-divide", "the denominator is never zero", by="Linh")
    assert labels(store, model, "use-divide") == ["Safe Ratio"]
    assert labels(store, model, "use-divide", open_only=False) == ["Bad Ratio", "Safe Ratio"]
    assert store.models()[0]["score"] >= before and C.version_check(store, model["version_id"])["accepted"] == 1
    divide = [f for f in C.findings_for(store, model["version_id"], "tag-bad-ratio") if f["rule_id"] == "use-divide"]
    assert divide[0]["accepted"] == 1 and divide[0]["reason"] == "the denominator is never zero"
    assert C.exceptions(store)[0]["reason"] == "the denominator is never zero"
    assert C.withdraw(store, model["id"], "tag-bad-ratio", "use-divide")
    assert labels(store, model, "use-divide") == ["Bad Ratio", "Safe Ratio"]


def test_reports_without_a_model_are_not_scored(tmp_path, pbit):
    reports = Path(pbit).parent
    store = Store(tmp_path / "ws.db")
    shutil.copy(reports / "sales.pbix", tmp_path / "Report.pbix")   # its model is a stand-in blob
    scan_folders(store, [tmp_path])
    m = store.models()[0]
    assert m["status"] == "partial" and m["score"] is None and C.version_check(store, m["version_id"]) is None


def test_review_shows_new_and_fixed_findings(ws, pbit):
    store, model, folder = ws
    def fix_and_break(schema, layout):
        TROUBLE(schema, layout)
        found = measures(schema)
        found["Bad Ratio"][1]["expression"] = "DIVIDE ( [Total Sales], [Order Count] )"
        add("Measures", "Another Bad", "[Total Cost] / 2")(schema, layout)
    save(pbit, folder / "Sales.pbit", fix_and_break, bump=60)
    scan_folders(store)
    m = store.models()[0]
    delta = C.compare_checks(store, m["version_id"], store.previous_version(m["version_id"])["id"])
    new = {(f["rule_id"], f["label"]) for f in delta["new"]}
    fixed = {(f["rule_id"], f["label"]) for f in delta["fixed"]}
    assert ("use-divide", "Another Bad") in new and ("use-divide", "Bad Ratio") in fixed
    assert delta["previous"] is not None and not delta["previous_stale"]
    # the earlier version keeps the score it had under the rules of its day
    C.set_rule(store, "unused-measure", enabled=False)
    delta = C.compare_checks(store, m["version_id"], store.previous_version(m["version_id"])["id"])
    assert delta["previous_stale"]


def test_export_and_import(ws, tmp_path):
    store, _, _ = ws
    C.set_rule(store, "measure-folder", params={"min_measures": 3}, severity="warning")
    data = C.export_rules(store)
    other = Store(tmp_path / "other.db")
    assert C.import_rules(other, json.loads(json.dumps(data))) == len(RULES)
    rule = next(r for r in C.rule_set(other) if r["id"] == "measure-folder")
    assert (rule["params"]["min_measures"], rule["severity"]) == (3, "warning")
    with pytest.raises(ValueError):
        C.import_rules(other, {"rules": {"nope": {}}})
    with pytest.raises(ValueError):
        C.import_rules(other, {"something": 1})


def test_standards_page_data(ws):
    store, model, _ = ws
    store.describe_model(model["id"], owner="Linh")
    store.annotate(model["id"], "tag-bad-ratio", owner="Minh")
    data = C.standards(store)
    assert data["average"] == store.models()[0]["score"]
    owners = {o["owner"]: o for o in data["owners"]}
    assert set(owners) == {"Linh", "Minh"} and owners["Minh"]["total"] == len(
        [f for f in findings(store, model) if f["object_key"] == "tag-bad-ratio"])
    assert {f["label"] for f in C.owner_findings(store, "Minh")} == {"Bad Ratio"}
    divide = next(r for r in data["rules"] if r["id"] == "use-divide")
    assert (divide["open"], divide["models"]) == (2, 1)


def test_api(ws):
    store, model, _ = ws
    httpd = __import__("dax_lineage.workspace.app", fromlist=["make_server"]).make_server(store, port=0, quiet=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        status, data, _ = call(port, "GET", "/api/standards")
        assert status == 200 and data["models"] and data["rules"]
        assert call(port, "GET", f"/api/models/{model['id']}")[1]["check"]["findings"]
        hit = next(h for h in call(port, "GET", "/api/search?q=bad%20ratio")[1])
        obj = call(port, "GET", f"/api/objects/{hit['id']}")[1]
        assert {f["rule_id"] for f in obj["findings"]} >= {"use-divide", "measure-description"}
        assert all(f["title"] for f in obj["findings"])
        body = {"model_id": model["id"], "object_key": obj["object_key"], "rule_id": "use-divide"}
        assert call(port, "POST", "/api/exceptions", dict(body, reason=""))[0] == 400
        assert call(port, "POST", "/api/exceptions", dict(body, reason="fine", by="An"))[0] == 200
        assert call(port, "POST", "/api/exceptions", dict(body, remove=True))[1] == {"removed": True}
        assert call(port, "POST", "/api/rules/use-divide", {"enabled": False})[1]["enabled"] is False
        assert call(port, "POST", "/api/rules/use-divide", {"severity": "fatal"})[0] == 400
        assert call(port, "POST", "/api/rules/use-divide", {"params": [1]})[0] == 400
        assert call(port, "POST", "/api/rules/nope", {"enabled": True})[0] == 404
        exported = call(port, "GET", "/api/rules/export")[1]
        assert exported["rules"]["use-divide"]["enabled"] is False
        assert call(port, "POST", "/api/rules/reset", {})[0] == 200
        assert call(port, "POST", "/api/rules/import", exported)[1] == {"imported": len(RULES)}
        assert call(port, "POST", "/api/rules/use-divide", {"enabled": True}, {"Origin": "http://evil.example"})[0] == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cli(ws, capsys, tmp_path):
    store, _, _ = ws
    db = ["workspace", "--db", str(store.path)]
    assert daxlin(db + ["check"]) == 1                          # a broken reference is an error
    out = capsys.readouterr().out
    assert "broken-reference" in out and "Gone Measure" in out
    daxlin(db + ["check", "--min", "error"])
    assert "duplicate-dax" not in capsys.readouterr().out
    assert daxlin(db + ["rules", "set", "broken-reference", "--severity", "warning"]) == 0
    assert daxlin(db + ["check"]) == 0
    out_file = tmp_path / "rules.json"
    daxlin(db + ["rules", "export", "-o", str(out_file)])
    assert json.loads(out_file.read_text())["rules"]["broken-reference"]["severity"] == "warning"
    daxlin(db + ["rules", "reset"])
    assert daxlin(db + ["rules", "import", str(out_file)]) == 0
    daxlin(db + ["rules"])
    assert "broken-reference" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        daxlin(db + ["rules", "set", "nope", "--on"])
    daxlin(db + ["models"])
    assert "score" in capsys.readouterr().out
