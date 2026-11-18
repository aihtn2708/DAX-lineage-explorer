"""The workspace demo on the GitHub Pages site: built from made-up models, and kept in step with the workspace."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "workspace-demo"


def builder():
    spec = importlib.util.spec_from_file_location("build_workspace_demo", ROOT / "scripts" / "build_workspace_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_demo_builds_with_a_full_history(tmp_path):
    out = tmp_path / "demo"
    builder().build(out)
    data = json.loads((out / "data.json").read_text(encoding="utf-8"))
    answers = data["answers"]
    text = (out / "data.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in text and "/tmp/" not in text            # no path from the build machine
    summary = answers["/api/summary"]
    assert summary["models"] == 4 and summary["pending_reviews"] == 2 and summary["open_changes"] == 2
    assert {r["review"] for r in answers["/api/reviews?status=all"]} == {"pending", "approved", "changes_requested",
                                                                       "not_required"}
    assert {c["status"] for c in answers["/api/changes?status=all"]} == {"proposed", "approved", "confirmed"}
    assert {m["tier"] for m in answers["/api/models"]} == {"personal", "team", "certified"}
    assert any(not m["ok"] for m in answers["/api/policies"]["models"])
    assert any(m["counts"]["drift"] for m in answers["/api/metrics"]["metrics"])
    assert answers["/api/folders"]["folders"][0]["path"].startswith("C:\\Users\\demo\\")
    assert answers["/api/folders"]["can_browse"] is False
    # every page a link can reach was recorded
    for m in answers["/api/models"]:
        assert f"/api/models/{m['id']}" in answers
        for o in answers[f"/api/models/{m['id']}"]["objects"]:
            assert f"/api/objects/{o['id']}" in answers
    for r in answers["/api/reviews?status=all"]:
        assert f"/api/versions/{r['id']}" in answers and (out / "graphs" / f"v{r['id']}.json").exists()
    for c in answers["/api/changes?status=all"]:
        assert (out / "graphs" / f"c{c['id']}.json").exists()
    graph = json.loads((out / "graphs" / f"v{answers['/api/reviews?status=pending'][0]['id']}.json").read_text())
    assert graph["back"].startswith("index.html#/version/") and graph["graph"]["nodes"]
    assert {r["kind"] for r in data["search"]} == {"measure", "column"} and "expression" in data["search"][0]
    page = (out / "graph.html").read_text(encoding="utf-8")
    assert "connect-src 'self'" in page and "connect-src 'none'" not in page


def test_the_published_demo_matches_the_workspace():
    """docs/workspace-demo runs copies of the workspace page: rebuild it when the page changes."""
    static = ROOT / "dax_lineage" / "workspace" / "static"
    sources = ROOT / "scripts" / "workspace-demo"
    for name, source in (("app.js", static), ("app.css", static), ("index.html", sources), ("demo.js", sources),
                         ("demo.css", sources)):
        assert (DOCS / name).read_bytes() == (source / name).read_bytes(), \
            f"docs/workspace-demo/{name} is out of date: run python scripts/build_workspace_demo.py"
    html = (DOCS / "index.html").read_text(encoding="utf-8")
    assert "default-src 'none'" in html and "connect-src 'self'" in html
    assert html.index('src="demo.js"') < html.index('src="app.js"')    # the stand-in is in place first
