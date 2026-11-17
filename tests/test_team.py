"""Team workspace, milestone 5: Postgres, sign-in, roles, tokens, the audit log, backups."""
import gzip
import json
import threading

import pytest

from dax_lineage.cli import main as daxlin
from dax_lineage.workspace import Store, scan_folders
from dax_lineage.workspace import auth as A
from dax_lineage.workspace import changes as CH
from dax_lineage.workspace.app import AuthConfig, action_name, make_server, required_role, serve
from dax_lineage.workspace.backup import backup, restore
from dax_lineage.workspace.db import mask, translate, translate_ddl
from test_review import pbit, save  # noqa: F401  (pbit is a fixture)
from test_workspace import call

PASSWORD = "correct horse battery"


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
    for name, role in (("ada", "admin"), ("rev", "reviewer"), ("aut", "author"), ("vic", "viewer")):
        A.add_account(store, name, role, name.title(), PASSWORD)
    return store, model, keys


class Server:
    def __init__(self, store, **auth):
        self.httpd = make_server(store, port=0, quiet=True, auth=AuthConfig(**auth))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def __call__(self, method, path, body=None, cookie=None, headers=None):
        hdrs = dict(headers or {})
        if cookie:
            hdrs["Cookie"] = cookie
        return call(self.port, method, path, body, hdrs)

    def sign_in(self, name):
        status, _, response = self("POST", "/api/login", {"username": name, "password": PASSWORD})
        assert status == 200
        return response.getheader("Set-Cookie").split(";")[0]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def local(ws):
    server = Server(ws[0], mode="local")
    yield ws, server
    server.close()


# -- the database layer ----------------------------------------------------------------------

def test_sql_for_postgres():
    sql, table = translate("INSERT OR IGNORE INTO folder (path, added_at) VALUES (?, ?)")
    assert sql == "INSERT INTO folder (path, added_at) VALUES (%s, %s) ON CONFLICT DO NOTHING" and table == "folder"
    assert translate("SELECT 1 FROM x WHERE fold(name) LIKE ? ESCAPE '\\'")[0] == \
        "SELECT 1 FROM x WHERE lower(name) LIKE %s ESCAPE '\\'"
    assert translate("SELECT '100%' FROM x")[0] == "SELECT '100%%' FROM x"
    assert translate("INSERT INTO usage (a) VALUES (?)")[1] is None          # no id column
    ddl = translate_ddl("CREATE TABLE t (\n    id       INTEGER PRIMARY KEY,\n    version_id INTEGER PRIMARY KEY);")
    assert ddl == ["CREATE TABLE t (\n    id BIGSERIAL PRIMARY KEY,\n    version_id BIGINT PRIMARY KEY)"]
    assert mask("postgresql://u:secret@db/x") == "postgresql://u:•••@db/x" and mask("ws.db") == "ws.db"


# -- accounts ---------------------------------------------------------------------------------------

def test_accounts(ws):
    store, _, _ = ws
    with pytest.raises(ValueError):
        A.add_account(store, "ADA", "viewer")                      # names are one per person, any case
    with pytest.raises(ValueError):
        A.add_account(store, "new", "owner")
    with pytest.raises(ValueError):
        A.add_account(store, "new", "viewer", password="short")
    with pytest.raises(ValueError):
        A.update_account(store, "ada", role="viewer")              # the last admin stays admin
    A.update_account(store, "rev", role="admin")
    A.update_account(store, "ada", role="viewer")
    assert A.account(store, "ADA")["role"] == "viewer"
    stored = A.account(store, "aut")["password_hash"]
    assert stored.startswith("pbkdf2_sha256$") and PASSWORD not in stored
    assert A.check_password(PASSWORD, stored) and not A.check_password("nope", stored)
    assert A.sign_in(store, "aut", "nope") is None and A.sign_in(store, "ghost", PASSWORD) is None
    A.update_account(store, "aut", disabled=True)
    assert A.sign_in(store, "aut", PASSWORD) is None


# -- local sign-in ----------------------------------------------------------------------------------

def test_nothing_without_signing_in(local):
    (store, _, _), server = local
    assert server("GET", "/")[0] == 200 and server("GET", "/app.js")[0] == 200   # the sign-in page itself
    status, session, _ = server("GET", "/api/session")
    assert status == 200 and session["signed_in"] is False and session["mode"] == "local"
    assert server("GET", "/api/models")[0] == 401
    assert server("POST", "/api/annotations", {"model_id": 1, "object_key": "x", "note": "n"})[0] == 401
    assert server("GET", "/api/models", cookie="pbiw_session=made-up")[0] == 401


def test_sign_in_out_and_throttle(local):
    (store, _, _), server = local
    assert server("POST", "/api/login", {"username": "aut", "password": "wrong"})[0] == 401
    cookie = server.sign_in("aut")
    status, session, _ = server("GET", "/api/session", cookie=cookie)
    assert session["signed_in"] and session["user"]["role"] == "author"
    assert server("GET", "/api/models", cookie=cookie)[0] == 200
    _, _, response = server("POST", "/api/logout", {}, cookie=cookie)
    assert "Max-Age=0" in response.getheader("Set-Cookie")
    assert server("GET", "/api/models", cookie=cookie)[0] == 401
    for _ in range(5):
        server("POST", "/api/login", {"username": "vic", "password": "wrong"})
    assert server("POST", "/api/login", {"username": "vic", "password": PASSWORD})[0] == 429
    actions = [r["action"] for r in A.audit_log(store)]
    assert "sign-in.failed" in actions and "sign-in.blocked" in actions and "sign-in" in actions


def test_cookie_flags(ws):
    server = Server(ws[0], mode="local", secure_cookies=True)
    try:
        _, _, response = server("POST", "/api/login", {"username": "aut", "password": PASSWORD})
        flags = response.getheader("Set-Cookie")
        assert all(f in flags for f in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/"))
    finally:
        server.close()


def test_roles(local):
    (store, model, keys), server = local
    viewer, author, reviewer, admin = (server.sign_in(n) for n in ("vic", "aut", "rev", "ada"))
    note = {"model_id": model["id"], "object_key": keys["Sales QTD"], "note": "old", "by": "Mallory"}
    assert server("POST", "/api/annotations", note, cookie=viewer)[0] == 403
    assert server("POST", "/api/annotations", note, cookie=author)[0] == 200
    assert store.object(store.search("Sales QTD")[0]["id"])["annotation"]["updated_by"] == "Aut"  # not Mallory
    assert server("POST", "/api/rules/use-divide", {"enabled": False}, cookie=author)[0] == 403
    assert server("POST", "/api/rules/use-divide", {"enabled": False}, cookie=reviewer)[0] == 200
    assert server("POST", "/api/scan", {}, cookie=reviewer)[0] == 403
    assert server("GET", "/api/admin/users", cookie=reviewer)[0] == 403
    assert server("GET", "/api/admin/users", cookie=admin)[0] == 200
    assert any(r["action"] == "denied" for r in A.audit_log(store))


def test_no_approving_your_own_change(local):
    (store, model, keys), server = local
    author, reviewer, other_author = server.sign_in("aut"), server.sign_in("rev"), None
    A.add_account(store, "aut2", "author", "Aut Two", PASSWORD)
    other_author = server.sign_in("aut2")
    status, made, _ = server("POST", "/api/changes", {"model_id": model["id"], "items": [
        {"op": "remove", "object_key": keys["Sales QTD"]}], "by": "Someone Else"}, cookie=reviewer)
    assert status == 200 and made["created_by"] == "Rev"
    path = f"/api/changes/{made['id']}/decision"
    status, error, _ = server("POST", path, {"status": "approved"}, cookie=reviewer)
    assert status == 403 and "someone else" in error["error"]
    assert server("POST", path, {"status": "approved"}, cookie=author)[0] == 403          # an author cannot approve
    assert server("POST", path, {"status": "cancelled"}, cookie=other_author)[0] == 403  # nor cancel others' changes
    A.update_account(store, "ada", display_name="")
    assert server("POST", path, {"status": "approved"}, cookie=server.sign_in("ada"))[0] == 200
    assert CH.get(store, made["id"])["decided_by"] == "ada"


def test_admin_pages(local):
    (store, _, _), server = local
    admin = server.sign_in("ada")
    status, made, _ = server("POST", "/api/admin/users", {"create": True, "username": "New.Person", "role": "author",
                                                          "password": PASSWORD}, cookie=admin)
    assert status == 200 and made == {"username": "new.person", "display_name": "", "role": "author"}
    assert server("POST", "/api/admin/users", {"username": "new.person", "role": "reviewer"}, cookie=admin)[0] == 200
    assert server("POST", "/api/admin/users", {"username": "ada", "role": "viewer"}, cookie=admin)[0] == 400
    log = server("GET", "/api/admin/audit?action=admin", cookie=admin)[1]
    created = next(r for r in log if r["action"] == "admin.users" and '"create"' in r["detail"])
    assert "•••" in created["detail"] and PASSWORD not in json.dumps(log)


# -- tokens, for the companion -------------------------------------------------------------------

def test_tokens(local):
    (store, model, keys), server = local
    cookie = server.sign_in("aut")
    status, made, _ = server("POST", "/api/account/tokens", {"name": "laptop"}, cookie=cookie)
    token = made["token"]
    assert status == 200 and token.startswith("pbiw_")
    bearer = {"Authorization": f"Bearer {token}"}
    assert server("GET", "/api/changes/ready", headers=bearer)[0] == 200
    assert server("POST", "/api/annotations", {"model_id": model["id"], "object_key": keys["Sales QTD"], "note": "x"},
                  headers=bearer)[0] == 200
    listed = server("GET", "/api/account", cookie=cookie)[1]["tokens"]
    assert listed[0]["name"] == "laptop" and listed[0]["last_used_at"] and "token_hash" not in listed[0]
    assert server("POST", "/api/account/tokens/revoke", {"prefix": token[:12]}, cookie=cookie)[1] == {"revoked": True}
    assert server("GET", "/api/models", headers=bearer)[0] == 401
    assert server("GET", "/api/models", headers={"Authorization": "Bearer pbiw_guess"})[0] == 401


# -- Windows sign-in ---------------------------------------------------------------------------------

def test_iis_mode(ws):
    store, _, _ = ws
    seen = []

    def resolver(handle):
        seen.append(handle)
        if handle == "dead":
            raise OSError(6, "invalid handle")
        return "CONTOSO\\Linh"

    server = Server(store, mode="iis", token_resolver=resolver)
    try:
        assert server("GET", "/api/models")[0] == 401
        token = {"X-IIS-WindowsAuthToken": "2b4"}
        status, session, _ = server("GET", "/api/session", headers=token)
        assert session["signed_in"] and session["user"]["username"] == "contoso\\linh"
        assert session["user"]["role"] == "viewer" and seen == ["2b4"]            # made on the first visit
        assert server("GET", "/api/models", headers={"X-IIS-WindowsAuthToken": "dead"})[0] == 401
        assert server("GET", "/api/models", headers={"X-IIS-WindowsAuthToken": "not hex!"})[0] == 401
        A.update_account(store, "contoso\\linh", disabled=True)
        assert server("GET", "/api/models", headers=token)[0] == 401
    finally:
        server.close()
    elsewhere = Server(store, mode="iis", token_resolver=resolver, trusted_proxies=("10.1.2.3",))
    try:
        assert elsewhere("GET", "/api/models", headers={"X-IIS-WindowsAuthToken": "2b4"})[0] == 401
    finally:
        elsewhere.close()


def test_proxy_header_mode(ws):
    store, _, _ = ws
    server = Server(store, mode="windows", default_role="author")
    try:
        status, session, _ = server("GET", "/api/session", headers={"X-Remote-User": "rev"})
        assert session["user"]["role"] == "reviewer"                              # an existing account keeps its role
        status, session, _ = server("GET", "/api/session", headers={"X-Remote-User": "CONTOSO\\Hoa"})
        assert session["user"]["role"] == "author"
    finally:
        server.close()
    untrusted = Server(store, mode="windows", trusted_proxies=("10.1.2.3",))
    try:
        assert untrusted("GET", "/api/models", headers={"X-Remote-User": "ada"})[0] == 401
    finally:
        untrusted.close()


def test_serve_refuses_an_open_network_without_sign_in(ws, tmp_path, capsys):
    store, _, _ = ws
    assert serve(store, host="0.0.0.0", port=0, open_browser=False) == 2
    assert "Refusing" in capsys.readouterr().err
    empty = Store(tmp_path / "empty.db")
    assert serve(empty, port=0, open_browser=False, auth=AuthConfig(mode="local")) == 2


def test_required_roles_and_audit_names():
    assert required_role("/api/annotations", {}) == "author"
    assert required_role("/api/versions/3/review", {}) == "reviewer"
    assert required_role("/api/changes/3/decision", {"status": "cancelled"}) == "author"
    assert required_role("/api/changes/3/decision", {"status": "approved"}) == "reviewer"
    assert required_role("/api/admin/users", {}) == "admin" and required_role("/api/scan", {}) == "admin"
    assert action_name("/api/changes/12/decision") == ("changes.decision", "12")


# -- backups -----------------------------------------------------------------------------------------

def test_backup_and_restore(ws, tmp_path):
    store, model, keys = ws
    store.annotate(model["id"], keys["Sales QTD"], decision="remove", note="old", by="An")
    made = CH.create(store, model["id"], "", [{"op": "remove", "object_key": keys["Sales QTD"]}], by="An")
    out = tmp_path / "ws.jsonl.gz"
    counts = backup(store, out)
    assert counts["object"] > 0 and counts["account"] == 4 and counts["change_op"] == 1
    with gzip.open(out, "rt") as fh:
        assert json.loads(fh.readline())["format"] == "daxlin-workspace-backup"

    copy = Store(tmp_path / "copy.db")
    assert restore(copy, out)["object"] == counts["object"]
    assert [m["name"] for m in copy.models()] == ["Sales"]
    assert copy.decisions("remove")[0]["note"] == "old"
    assert CH.get(copy, made["id"])["title"] == made["title"]
    assert A.sign_in(copy, "aut", PASSWORD) is not None                         # passwords travel as hashes
    # new rows carry on after the restored ones
    again = CH.create(copy, model["id"], "", [{"op": "remove", "object_key": keys["Sales MTD"]}], by="An")
    assert again["id"] > made["id"]
    with pytest.raises(ValueError):
        restore(copy, out)                                                       # not into a workspace in use
    assert restore(copy, out, force=True)["model"] == 1
    with pytest.raises(ValueError):
        bad = tmp_path / "bad.gz"
        with gzip.open(bad, "wt") as fh:
            fh.write('{"format": "something else"}\n')
        restore(Store(tmp_path / "third.db"), bad)


def test_cli(ws, tmp_path, capsys, monkeypatch):
    store, _, _ = ws
    db = ["workspace", "--db", str(store.path)]
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSWORD)
    assert daxlin(db + ["users", "add", "Hoa", "--role", "reviewer", "--password"]) == 0
    assert A.sign_in(store, "hoa", PASSWORD) is not None
    assert daxlin(db + ["users", "set", "hoa", "--disable"]) == 0
    daxlin(db + ["users"])
    assert "disabled" in capsys.readouterr().out
    assert daxlin(db + ["tokens", "create", "aut", "--name", "companion"]) == 0
    token = capsys.readouterr().out.split()[0]
    assert A.token_account(store, token)["username"] == "aut"
    assert daxlin(db + ["tokens", "revoke", token[:12]]) == 0 and A.token_account(store, token) is None
    daxlin(db + ["audit"])
    out = capsys.readouterr().out
    assert "admin.users.add" in out and "tokens.create" in out and "command line" in out
    with pytest.raises(SystemExit):
        daxlin(db + ["users", "set", "nobody", "--role", "admin"])
    target = tmp_path / "b.jsonl.gz"
    assert daxlin(db + ["backup", "--out", str(target)]) == 0
    assert daxlin(["workspace", "--db", str(tmp_path / "restored.db"), "restore", str(target)]) == 0
    assert "restored into" in capsys.readouterr().out
