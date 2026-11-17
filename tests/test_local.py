"""Running the explorer locally: the offline page, the local server, the policies."""
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from dax_lineage.cli import main
from dax_lineage.offline import build, packaged
from dax_lineage.serve import make_server

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def test_offline_copies_are_up_to_date():
    fresh = build(DOCS)
    for path in (DOCS / "dax-lineage-explorer.html", ROOT / "dax_lineage" / "web" / "dax-lineage-explorer.html"):
        assert path.read_text(encoding="utf-8") == fresh, f"{path.name} is stale: run scripts/build_offline.py"


def test_offline_page_loads_nothing_from_anywhere():
    html = packaged()
    assert "<script src" not in html and 'rel="stylesheet"' not in html
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert all(r.startswith(("data:", "#", "https://github.com/")) for r in refs), refs
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html).group(1)
    assert "connect-src 'none'" in csp and "default-src 'none'" in csp
    assert "window.DAX_LINEAGE_EMBEDDED" in html, "samples travel inside the page"


def test_site_and_cli_pages_carry_a_policy():
    site = (DOCS / "index.html").read_text(encoding="utf-8")
    assert "connect-src 'self'" in site and "script-src 'self'" in site
    assert not re.search(r'<script src="(?:https?:)?//', site), "no third-party scripts"
    template = (ROOT / "dax_lineage" / "template.html").read_text(encoding="utf-8")
    assert "connect-src 'none'" in template


def test_cli_offline_writes_the_page(tmp_path):
    out = tmp_path / "explorer.html"
    assert main(["offline", "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == packaged()


@pytest.fixture
def server():
    srv = make_server("127.0.0.1", 0, quiet=True, allow_hosts=["lineage.corp.example"])
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def request(port, path="/", method="GET", host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=b"x" if method == "POST" else None)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, dict(res.headers), res.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def test_server_serves_the_page_with_hardening_headers(server):
    status, headers, body = request(server)
    assert status == 200 and body.decode("utf-8") == packaged()
    assert "connect-src 'none'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "no-store"


def test_server_accepts_nothing_and_serves_nothing_else(server):
    assert request(server, method="POST")[0] == 405
    assert request(server, "/../../etc/passwd")[0] == 404
    assert request(server, "/samples/info_sample.xlsx")[0] == 404


def test_server_refuses_other_host_names(server):
    assert request(server, host="evil.example")[0] == 421
    assert request(server, host=f"localhost:{server}")[0] == 200
    assert request(server, host="lineage.corp.example")[0] == 200, "--allow-host"


def test_server_binds_to_loopback_by_default():
    srv = make_server(port=0, quiet=True)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()
