"""Governance G1: the lineage explorer's graph inside the workspace, for any stored version."""
import json
import re
import shutil
import threading
from pathlib import Path

import pytest

from dax_lineage.graph import build_graph
from dax_lineage.load import catalogue_from_rows
from dax_lineage.pbi_report import read_report
from dax_lineage.workspace import Store, scan_folders
from dax_lineage.workspace import auth as A
from dax_lineage.workspace import changes as CH
from dax_lineage.workspace.app import AuthConfig, make_server
from dax_lineage.workspace.lineage import graph_for_version, review_marks
from test_review import measures, pbit, save, the_save  # noqa: F401  (pbit is a fixture)
from test_workspace import call


@pytest.fixture
def ws(tmp_path, pbit):
    folder = tmp_path / "BI"
    folder.mkdir()
    store = Store(tmp_path / "ws.db")
    store.add_folder(folder)
    save(pbit, folder / "Sales.pbit")
    scan_folders(store)
    return store, folder


def by_id(graph):
    return {n["id"]: n for n in graph["nodes"]}


def test_rebuilt_graph_is_the_scanners_graph(ws, pbit, tmp_path):
    """What the workspace stores is enough to redraw exactly what the explorer drew for the file."""
    store, folder = ws
    shutil.copy(Path(pbit).parent / "problems.pbit", folder / "Problems.pbit")
    sources = [folder / "Sales.pbit", folder / "Problems.pbit"]
    scan_folders(store)
    for model, source in zip(sorted(store.models(), key=lambda m: m["name"] != "Sales"), sources):
        result = read_report(source)
        expected = build_graph(catalogue_from_rows(result.model), usages=result.usages).to_dict()
        rebuilt = graph_for_version(store, model["version_id"])
        assert by_id(rebuilt) == by_id(expected), model["name"]
        key = lambda e: json.dumps(e, sort_keys=True)
        assert sorted(map(key, rebuilt["edges"])) == sorted(map(key, expected["edges"]))
        assert rebuilt["stats"] == expected["stats"] and rebuilt["tables"] == expected["tables"]


def test_marks_for_a_review(ws, pbit):
    store, folder = ws
    save(pbit, folder / "Sales.pbit", the_save, bump=60)
    scan_folders(store)
    version = store.models()[0]["version_id"]
    marks = review_marks(store, version)
    nodes = by_id(graph_for_version(store, version, marks))
    named = {nodes[node]["name"]: mark for node, mark in marks.items()}
    assert named["Net Sales"] == "renamed" and named["Sales Target"] == "added"
    assert named["Sales PY"] == "edited" and named["Gross Margin"] == "follows"
    assert named["Gross Margin %"] == "broken" and not nodes[[k for k in marks if nodes[k]["name"] == "Gross Margin %"][0]]["exists"]
    assert sum(1 for n in nodes.values() if "mark" in n) == len(marks)


def test_page_and_api(ws):
    store, folder = ws
    model = store.models()[0]
    keys = {o["name"]: o for o in store.model(model["id"])["objects"] if o["kind"] == "measure"}
    made = CH.create(store, model["id"], "", [{"op": "rename", "object_key": keys["Customer Count"]["object_key"],
                                               "value": "Customers"}], by="An")
    httpd = make_server(store, port=0, quiet=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        status, body, response = call(port, "GET", f"/explorer/version/{model['version_id']}?focus=m%3Atotal%20sales")
        page = body.decode("utf-8")
        assert status == 200 and "workspaceGraph" in page and "LineageViewer.mount" in page
        policies = response.headers.get_all("Content-Security-Policy")
        assert len(policies) == 1 and "connect-src 'none'" in policies[0] and "frame-ancestors 'none'" in policies[0]
        data = json.loads(re.search(r'id="workspaceGraph">(.*?)</script>', page, re.S).group(1))
        assert data["focus"] == "m:total sales" and data["back"] == f"/#/model/{model['id']}"
        status, body, _ = call(port, "GET", f"/explorer/version/{model['version_id']}?change={made['id']}")
        data = json.loads(re.search(r'id="workspaceGraph">(.*?)</script>', body.decode(), re.S).group(1))
        marked = {n["name"]: n["mark"] for n in data["graph"]["nodes"] if "mark" in n}
        assert marked == {"Customer Count": "touched", "Sales per Customer": "follows"}
        assert data["back"] == f"/#/change/{made['id']}"
        assert call(port, "GET", "/explorer/version/99999")[0] == 404
        assert call(port, "GET", f"/explorer/version/{model['version_id']}?change=99999")[0] == 404
        status, graph, _ = call(port, "GET", f"/api/versions/{model['version_id']}/lineage")
        assert status == 200 and graph["stats"]["measures"] == model["measures"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_model_text_cannot_break_out_of_the_page(ws, pbit):
    store, folder = ws
    hostile = '</script><script>alert(1)</script><!--'
    def edit(schema, layout):
        table, m = measures(schema)["Total Cost"]
        table["measures"].append({"name": "Odd", "expression": f'IF ( 1 < 2, "{hostile}" )', "lineageTag": "tag-odd"})
    save(pbit, folder / "Sales.pbit", edit, bump=60)
    scan_folders(store)
    httpd = make_server(store, port=0, quiet=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        page = call(httpd.server_address[1], "GET", f"/explorer/version/{store.models()[0]['version_id']}")[1].decode()
        payload = re.search(r'id="workspaceGraph">(.*?)</script>', page, re.S).group(1)
        assert "<" not in payload and hostile not in page
        assert any(n["name"] == "Odd" and hostile in n["expression"] for n in json.loads(payload)["graph"]["nodes"])
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_graph_needs_sign_in(ws):
    store, _ = ws
    A.add_account(store, "vic", "viewer", "", "correct horse battery")
    httpd = make_server(store, port=0, quiet=True, auth=AuthConfig(mode="local"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    version = store.models()[0]["version_id"]
    try:
        status, _, response = call(port, "GET", f"/explorer/version/{version}")
        assert status == 303 and response.getheader("Location") == "/#/signin"
        assert call(port, "GET", f"/api/versions/{version}/lineage")[0] == 401
        _, _, response = call(port, "POST", "/api/login", {"username": "vic", "password": "correct horse battery"})
        cookie = {"Cookie": response.getheader("Set-Cookie").split(";")[0]}
        assert call(port, "GET", f"/explorer/version/{version}", None, cookie)[0] == 200   # viewers may look
    finally:
        httpd.shutdown()
        httpd.server_close()
