"""Choosing the folders to scan from the page."""
import threading
import time

import pytest

from dax_lineage.workspace import Store
from dax_lineage.workspace import app as app_module
from dax_lineage.workspace.app import AuthConfig, make_server
from test_review import pbit, save  # noqa: F401  (pbit is a fixture)
from test_workspace import call


@pytest.fixture
def server(tmp_path):
    store = Store(tmp_path / "ws.db")
    httpd = make_server(store, port=0, quiet=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield store, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def wait_for_scan(port):
    for _ in range(200):
        state = call(port, "GET", "/api/scan")[1]
        if not state["running"]:
            return state
        time.sleep(0.05)
    raise AssertionError("the scan did not finish")


def test_add_and_scan_from_the_page(server, tmp_path, pbit):
    store, port = server
    folder = tmp_path / "Power BI project"
    (folder / "sub").mkdir(parents=True)
    save(pbit, folder / "sub" / "Sales.pbit")
    status, info, _ = call(port, "GET", "/api/folders")
    assert status == 200 and info["folders"] == [] and info["can_browse"] is True
    assert call(port, "POST", "/api/folders", {"path": ""})[0] == 400
    status, error, _ = call(port, "POST", "/api/folders", {"path": str(tmp_path / "nope")})
    assert status == 400 and "address bar" in error["error"]
    # a path pasted with the quotes File Explorer's "Copy as path" adds
    status, added, _ = call(port, "POST", "/api/folders", {"path": f'"{folder}"', "scan": True})
    assert status == 200 and added["scanning"] is True and added["folders"][0]["exists"]
    log = wait_for_scan(port)["log"]
    assert any("Sales.pbit" in line for line in log)
    assert call(port, "GET", "/api/folders")[1]["folders"][0]["models"] == 1
    assert [m["name"] for m in store.models()] == ["Sales"]
    status, after, _ = call(port, "POST", "/api/folders/remove", {"path": str(folder)})
    assert status == 200 and after["folders"] == [] and store.models()      # history stays
    assert call(port, "POST", "/api/folders/remove", {"path": str(folder)})[0] == 404


def test_folder_picker(server, tmp_path, monkeypatch):
    store, port = server
    monkeypatch.setattr(app_module, "pick_folder", lambda: str(tmp_path))
    status, picked, _ = call(port, "POST", "/api/folders/browse", {})
    assert status == 200 and picked == {"path": str(tmp_path)}
    monkeypatch.setattr(app_module, "pick_folder", lambda: (_ for _ in ()).throw(ValueError("no tkinter")))
    assert call(port, "POST", "/api/folders/browse", {})[0] == 400


def test_picker_only_for_someone_at_this_pc(tmp_path, monkeypatch):
    """With sign-in the server may be shared, and a dialog would open on its screen, not the user's."""
    from dax_lineage.workspace import auth as A
    store = Store(tmp_path / "ws.db")
    A.add_account(store, "ada", "admin", "", "correct horse battery")
    monkeypatch.setattr(app_module, "pick_folder", lambda: pytest.fail("opened a dialog"))
    httpd = make_server(store, port=0, quiet=True, auth=AuthConfig(mode="local"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        _, _, response = call(port, "POST", "/api/login", {"username": "ada", "password": "correct horse battery"})
        cookie = {"Cookie": response.getheader("Set-Cookie").split(";")[0]}
        assert call(port, "GET", "/api/folders", None, cookie)[1]["can_browse"] is False
        assert call(port, "POST", "/api/folders/browse", {}, cookie)[0] == 403
        A.add_account(store, "vic", "viewer", "", "correct horse battery")
        _, _, response = call(port, "POST", "/api/login", {"username": "vic", "password": "correct horse battery"})
        viewer = {"Cookie": response.getheader("Set-Cookie").split(";")[0]}
        assert call(port, "POST", "/api/folders", {"path": str(tmp_path)}, viewer)[0] == 403   # admins only
    finally:
        httpd.shutdown()
        httpd.server_close()
