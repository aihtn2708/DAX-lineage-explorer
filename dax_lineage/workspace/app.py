"""The workspace web app: a JSON API over the database and one page that uses it.

    daxlin workspace serve                    # http://127.0.0.1:8766
    daxlin workspace serve --read-only        # nobody can change notes or start a scan

Same hardening as ``daxlin serve``, plus what a server that accepts writes needs:
- listens on 127.0.0.1 unless told otherwise
- refuses a Host header that is not this server (DNS rebinding)
- writes must be POST with a JSON body: a page on another site cannot send that
  without a CORS preflight, and preflights are refused; an Origin header, when
  sent, must be this server
- the page loads its script and styles only from this server, and may only
  call this server (Content-Security-Policy)
- request bodies are capped at 64 KB; data from .pbix files is only ever put
  into the page as text, never as HTML

There are no accounts until milestone 5: anyone who can open the page can add
notes. Keep it on 127.0.0.1, or behind a proxy that signs people in.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from ..serve import LOOPBACK
from dataclasses import dataclass

from . import auth as A
from . import changes as change_requests
from . import check as standards_check
from .diff import compare
from .scan import scan_folders
from .store import REVIEW_STATES, Store

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "font-src 'none'; object-src 'none'; frame-src 'none'; worker-src 'none'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")
HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), usb=(), payment=(), clipboard-read=()",
    "Cache-Control": "no-store",
}
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}
MAX_BODY = 64 * 1024


def _static(name: str) -> bytes:
    return resources.files(__package__).joinpath("static", name).read_bytes()


class ScanJob:
    """At most one scan at a time, started from the page or the schedule."""

    def __init__(self, store: Store):
        self.store, self.lock = store, threading.Lock()
        self.running, self.lines = False, []

    def start(self) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running, self.lines = True, []
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        try:
            scan_folders(self.store, log=self.lines.append)
        except Exception as err:  # reported to the page, never raised into the server
            self.lines.append(f"scan stopped: {err}")
        finally:
            self.running = False

    def state(self) -> dict:
        return {"running": self.running, "log": self.lines[-200:]}


@dataclass
class AuthConfig:
    """How people sign in; see auth.py."""
    mode: str = "none"
    trusted_proxies: tuple = ("127.0.0.1", "::1")
    user_header: str = "X-Remote-User"
    default_role: str = "viewer"
    allow_self_approval: bool = False
    session_hours: int = 12
    secure_cookies: bool = False
    token_resolver: Optional[object] = None  # iis: hex token handle -> DOMAIN\user (A.iis_token_user)


COOKIE = "pbiw_session"


def required_role(path: str, body: dict) -> str:
    """The least role that may make this POST."""
    if path == "/api/scan" or path.startswith("/api/admin/") or path.startswith("/api/folders"):
        return "admin"
    if path.startswith("/api/account/"):
        return "viewer"
    if re.fullmatch(r"/api/versions/\d+/review", path) or path.startswith("/api/rules/") or path == "/api/exceptions":
        return "reviewer"
    if re.fullmatch(r"/api/changes/\d+/decision", path):
        return "author" if body.get("status") == "cancelled" else "reviewer"
    return "author"


def action_name(path: str) -> tuple:
    """("change.decision", "12") from /api/changes/12/decision, for the audit log."""
    parts = [p for p in path.split("/") if p and p != "api"]
    target = ",".join(p for p in parts if p.isdigit())
    words = [p for p in parts if not p.isdigit()]
    return ".".join(words) or "request", target


PICKER = r"""
import sys, tkinter
from tkinter import filedialog
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
path = filedialog.askdirectory(title="Choose the folder with your Power BI files", mustexist=True)
root.destroy()
sys.stdout.write(path or "")
"""


def pick_folder() -> str:
    """Open the system's folder dialog on this computer; '' if cancelled."""
    import subprocess
    try:
        done = subprocess.run([sys.executable, "-c", PICKER], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("the folder picker did not open: paste the path instead") from None
    if done.returncode != 0:
        raise ValueError("the folder picker could not open here (Python's tkinter is missing): paste the path instead")
    path = done.stdout.strip()
    return str(Path(path)) if path else ""


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def make_handler(store: Store, allowed_hosts: set, read_only: bool, quiet: bool, job=None,
                 auth: Optional[AuthConfig] = None):
    job = job or ScanJob(store)
    auth = auth or AuthConfig()
    throttle = A.Throttle()

    class Handler(BaseHTTPRequestHandler):
        server_version = "daxlin-workspace"
        sys_version = ""

        # -- plumbing -----------------------------------------------------

        def _send(self, status: int, body: bytes, content_type: str, extra=()):
            self.send_response(status)
            for name, value in HEADERS.items():
                self.send_header(name, value)
            for name, value in extra:
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload, extra=()):
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", extra)

        # -- who is asking ---------------------------------------------------

        def _cookie(self) -> str:
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except Exception:
                return ""
            return jar[COOKIE].value if COOKIE in jar else ""

        def _identify(self):
            """(account or None, signed): signed means the name comes from a sign-in, not from the page."""
            header = (self.headers.get("Authorization") or "").strip()
            if header.lower().startswith("bearer "):
                found = A.token_account(store, header[7:].strip())
                return found, found is not None
            if auth.mode == "none":
                return dict(A.LOCAL_ADMIN), False
            if auth.mode == "local":
                found = A.session_account(store, self._cookie())
                return found, found is not None
            if self.client_address[0] not in auth.trusted_proxies:
                return None, False
            if auth.mode == "iis":
                handle = (self.headers.get(A.IIS_TOKEN_HEADER) or "").strip()
                if not re.fullmatch(r"[0-9A-Fa-f]{1,16}", handle):
                    return None, False
                try:
                    name = (auth.token_resolver or A.iis_token_user)(handle)
                except OSError:
                    return None, False
            else:
                name = (self.headers.get(auth.user_header) or "").strip()
            if name:
                found = A.windows_account(store, name, auth.default_role)
                return found, found is not None
            return None, False

        def _session_cookie(self, token: str, max_age: int) -> tuple:
            flags = f"; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}" + ("; Secure" if auth.secure_cookies else "")
            return ("Set-Cookie", f"{COOKIE}={token}{flags}")

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").strip().lower() in allowed_hosts

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            parts = urlsplit(origin.strip().lower())
            return parts.scheme in ("http", "https") and parts.netloc in allowed_hosts

        def _body(self) -> dict:
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send JSON")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too large")
            if getattr(self, "_parsed", None) is not None:
                return self._parsed
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise ApiError(HTTPStatus.BAD_REQUEST, "not valid JSON") from None
            if not isinstance(data, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "send a JSON object")
            if getattr(self, "signed", False):
                data["by"] = A.name_of(self.who)  # a signed-in name, not whatever the page says
            self._parsed = data
            return data

        # -- methods ------------------------------------------------------

        def do_GET(self):
            if not self._host_ok():
                return self._send(HTTPStatus.MISDIRECTED_REQUEST, b"unknown host\n", "text/plain; charset=utf-8")
            parts = urlsplit(self.path)
            if parts.path in STATIC:
                name, content_type = STATIC[parts.path]
                return self._send(HTTPStatus.OK, _static(name), content_type)
            if not parts.path.startswith("/api/"):
                return self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")
            self.who, self.signed = self._identify()
            if parts.path == "/api/session":
                return self._json(HTTPStatus.OK, self._session())
            if self.who is None:
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "sign in first", "signin": auth.mode})
            if parts.path.startswith("/api/admin/") and A.rank(self.who["role"]) < A.rank("admin"):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "only an admin can see this"})
            self._api(lambda: self._get(parts.path, parse_qs(parts.query)))

        do_HEAD = do_GET

        def do_POST(self):
            if not self._host_ok():
                return self._send(HTTPStatus.MISDIRECTED_REQUEST, b"unknown host\n", "text/plain; charset=utf-8")
            if not self._origin_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "request from another site refused"})
            path = urlsplit(self.path).path
            self._parsed = None
            self.who, self.signed = self._identify()
            if path == "/api/login":
                return self._login()
            if path == "/api/logout":
                A.sign_out(store, self._cookie())
                return self._json(HTTPStatus.OK, {"ok": True}, [self._session_cookie("", 0)])
            if read_only:
                return self._json(HTTPStatus.FORBIDDEN, {"error": "this workspace is read-only"})
            if self.who is None:
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "sign in first", "signin": auth.mode})
            try:
                body = self._body()
            except ApiError as err:
                return self._json(err.status, {"error": str(err)})
            need = required_role(path, body)
            if A.rank(self.who["role"]) < A.rank(need):
                A.audit(store, self.who, "denied", path, {"needs": need}, self.client_address[0])
                return self._json(HTTPStatus.FORBIDDEN, {"error": f"this needs the {need} role; you are {self.who['role']}"})
            refusal = self._refuse_own_approval(path, body)
            if refusal:
                return self._json(HTTPStatus.FORBIDDEN, {"error": refusal})
            status = self._api(lambda: self._post(path))
            if status < 400 and not path.endswith("/preview"):  # a preview changes nothing
                action, target = action_name(path)
                A.audit(store, self.who, action, target, body, self.client_address[0])

        def _can_browse(self) -> bool:
            """A folder dialog opens on this computer's screen: only for someone sitting at it."""
            return (auth.mode == "none" and self.client_address[0] in ("127.0.0.1", "::1")
                    and self.server.server_address[0] in ("127.0.0.1", "::1", "localhost"))

        def _refuse_own_approval(self, path: str, body: dict) -> str:
            """Someone else approves a change, and only its author or a reviewer cancels it."""
            match = re.fullmatch(r"/api/changes/(\d+)/decision", path)
            if not match or not self.signed:
                return ""
            request = change_requests.get(store, int(match.group(1)))
            if request is None:
                return ""
            mine = request["created_by"] in (self.who["username"], A.name_of(self.who))
            if body.get("status") == "approved" and mine and not auth.allow_self_approval:
                return "you proposed this change: someone else must approve it"
            if body.get("status") == "cancelled" and not mine and A.rank(self.who["role"]) < A.rank("reviewer"):
                return "only its author or a reviewer can cancel a change"
            return ""

        def _session(self) -> dict:
            who = self.who
            return {"mode": auth.mode, "signed_in": who is not None and (self.signed or auth.mode == "none"),
                    "user": ({"username": who["username"], "display_name": who["display_name"], "role": who["role"]}
                             if who else None),
                    "read_only": read_only, "allow_self_approval": auth.allow_self_approval or auth.mode == "none"}

        def _login(self):
            if auth.mode != "local":
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "this workspace does not use passwords"})
            try:
                body = self._body()
            except ApiError as err:
                return self._json(err.status, {"error": str(err)})
            username, address = str(body.get("username", "")), self.client_address[0]
            if throttle.blocked(username, address):
                A.audit(store, {"username": username}, "sign-in.blocked", "", None, address)
                return self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "too many failed attempts: wait 15 minutes"})
            result = A.sign_in(store, username, str(body.get("password", "")), auth.session_hours)
            if result is None:
                throttle.failed(username, address)
                A.audit(store, {"username": username}, "sign-in.failed", "", None, address)
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "wrong name or password"})
            token, who = result
            throttle.cleared(username)
            A.audit(store, who, "sign-in", "", None, address)
            return self._json(HTTPStatus.OK, {"user": who}, [self._session_cookie(token, auth.session_hours * 3600)])

        def _refuse(self):
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"not allowed\n", "text/plain; charset=utf-8")

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

        def _api(self, call):
            try:
                status, payload = call()
            except ApiError as err:
                status, payload = err.status, {"error": str(err)}
            except change_requests.Blocked as err:
                status, payload = HTTPStatus.BAD_REQUEST, {"error": str(err), "problems": err.problems}
            except KeyError as err:
                message = str(err).strip("'\"")
                status = HTTPStatus.NOT_FOUND if message.startswith("no such") else HTTPStatus.BAD_REQUEST
                payload = {"error": message}
            except ValueError as err:
                status, payload = HTTPStatus.BAD_REQUEST, {"error": str(err)}
            self._json(status, payload)
            return int(status)

        # -- routes -------------------------------------------------------

        def _get(self, path: str, query: dict):
            one = lambda name, default="": (query.get(name) or [default])[0]
            if path == "/api/summary":
                return HTTPStatus.OK, dict(store.summary(), read_only=read_only, scan=job.state())
            if path == "/api/models":
                return HTTPStatus.OK, store.models()
            if path == "/api/search":
                kinds = tuple(k for k in one("kind", "measure,column").split(",") if k in ("measure", "column", "table"))
                return HTTPStatus.OK, store.search(one("q"), kinds or ("measure", "column"), one("dax") == "1")
            if path == "/api/decisions":
                return HTTPStatus.OK, store.decisions(one("decision", "remove"))
            if path == "/api/scan":
                return HTTPStatus.OK, job.state()
            if path == "/api/folders":
                return HTTPStatus.OK, {"folders": store.folders(), "can_browse": self._can_browse()}
            if path == "/api/reviews":
                status = one("status", "pending")
                if status not in REVIEW_STATES + ("all",):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "unknown review status")
                rows = store.reviews(status)
                for row in rows:
                    row["changes"] = compare(store, row["id"])["counts"]
                return HTTPStatus.OK, rows
            if path == "/api/standards":
                return HTTPStatus.OK, standards_check.standards(store)
            if path == "/api/standards/owner":
                return HTTPStatus.OK, standards_check.owner_findings(store, one("name"))
            if path == "/api/admin/users":
                return HTTPStatus.OK, A.accounts(store)
            if path == "/api/admin/audit":
                return HTTPStatus.OK, A.audit_log(store, int(one("limit", "200") or 200), one("user"), one("action"))
            if path == "/api/account":
                tokens = A.tokens(store, self.who["username"]) if self.signed else []
                return HTTPStatus.OK, {"user": self.who, "tokens": tokens, "mode": auth.mode}
            if path == "/api/changes/ready":
                return HTTPStatus.OK, {"changes": change_requests.ready(store)}
            if path == "/api/changes":
                model = one("model")
                return HTTPStatus.OK, change_requests.requests(store, one("status", "open"),
                                                               int(model) if model.isdigit() else None)
            match = re.fullmatch(r"/api/changes/(\d+)", path)
            if match:
                found = change_requests.get(store, int(match.group(1)))
                if found is None:
                    raise ApiError(HTTPStatus.NOT_FOUND, "not found")
                return HTTPStatus.OK, found
            if path == "/api/rules/export":
                return HTTPStatus.OK, standards_check.export_rules(store)
            match = re.fullmatch(r"/api/versions/(\d+)", path)
            if match:
                against = one("against")
                if against and not against.isdigit():
                    raise ApiError(HTTPStatus.BAD_REQUEST, "against must be a version id")
                review = compare(store, int(match.group(1)), int(against) if against else None)
                review["comments"] = store.comments(int(match.group(1)))
                review["standards"] = standards_check.compare_checks(
                    store, int(match.group(1)), review["against"]["id"] if review["against"] else None)
                review["versions"] = store.versions_of(review["version"]["model_id"])
                return HTTPStatus.OK, review
            match = re.fullmatch(r"/api/(models|objects)/(\d+)", path)
            if match:
                found = (store.model if match.group(1) == "models" else store.object)(int(match.group(2)))
                if found is None:
                    raise ApiError(HTTPStatus.NOT_FOUND, "not found")
                if match.group(1) == "models":
                    found["check"] = standards_check.version_check(store, found["version_id"]) \
                        if found["version_id"] else None
                else:
                    titles = {r["id"]: r["title"] for r in standards_check.rule_set(store)}
                    found["findings"] = [dict(f, title=titles.get(f["rule_id"], f["rule_id"])) for f in
                                         standards_check.findings_for(store, found["version_id"], found["object_key"])]
                return HTTPStatus.OK, found
            raise ApiError(HTTPStatus.NOT_FOUND, "not found")

        def _post(self, path: str):
            if path == "/api/scan":
                self._body()
                started = job.start()
                return (HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT), job.state()
            if path == "/api/annotations":
                body = self._body()
                saved = store.annotate(int(body["model_id"]), str(body["object_key"]), str(body.get("owner", "")),
                                       str(body.get("note", "")), str(body.get("decision", "")),
                                       str(body.get("by", "")))
                return HTTPStatus.OK, saved
            if path == "/api/admin/users":
                body = self._body()
                username = str(body.get("username", ""))
                if body.get("create"):
                    made = A.add_account(store, username, str(body.get("role") or "viewer"),
                                         str(body.get("display_name") or ""), body.get("password") or None)
                    return HTTPStatus.OK, {k: made[k] for k in ("username", "display_name", "role")}
                changed = A.update_account(store, username, body.get("role"), body.get("display_name"),
                                           body.get("password") or None, body.get("disabled"))
                return HTTPStatus.OK, {k: changed[k] for k in ("username", "display_name", "role", "disabled")}
            if path.startswith("/api/account/"):
                if not self.signed:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "this workspace has no sign-in, so there is no account")
                body = self._body()
                if path == "/api/account/password":
                    if auth.mode != "local":
                        raise ApiError(HTTPStatus.BAD_REQUEST, "passwords are not used here")
                    mine = A.account(store, self.who["username"])
                    if not A.check_password(str(body.get("old_password", "")), mine["password_hash"]):
                        raise ApiError(HTTPStatus.FORBIDDEN, "the current password is wrong")
                    A.update_account(store, self.who["username"], password=str(body.get("new_password", "")))
                    return HTTPStatus.OK, {"ok": True}
                if path == "/api/account/tokens":
                    return HTTPStatus.OK, {"token": A.create_token(store, self.who["username"], str(body.get("name", "")))}
                if path == "/api/account/tokens/revoke":
                    return HTTPStatus.OK, {"revoked": A.revoke_token(store, str(body.get("prefix", "")), self.who["username"])}
            if path in ("/api/changes", "/api/changes/preview"):
                body = self._body()
                items = body.get("items")
                if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "items must be a list of edits")
                if path.endswith("preview"):
                    planned = change_requests.plan(store, int(body["model_id"]), items)
                    planned.pop("model", None)
                    return HTTPStatus.OK, planned
                return HTTPStatus.OK, change_requests.create(store, int(body["model_id"]), str(body.get("title", "")),
                                                             items, str(body.get("by", "")))
            match = re.fullmatch(r"/api/changes/(\d+)/(decision|comments|applied)", path)
            if match:
                body, request_id = self._body(), int(match.group(1))
                if match.group(2) == "decision":
                    return HTTPStatus.OK, change_requests.decide(store, request_id, str(body.get("status", "")),
                                                                 str(body.get("by", "")), str(body.get("note", "")))
                if match.group(2) == "comments":
                    return HTTPStatus.OK, change_requests.comment(store, request_id, str(body.get("body", "")),
                                                                  str(body.get("by", "")))
                results = body.get("results")
                if not isinstance(results, list):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "results must be a list")
                return HTTPStatus.OK, change_requests.applied(store, request_id, results, str(body.get("by", "")),
                                                              str(body.get("machine", "")))
            if path == "/api/folders":
                folder = str(self._body().get("path", "")).strip().strip('"')
                if not folder:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "paste the folder's path first")
                if not Path(folder).expanduser().is_dir():
                    raise ApiError(HTTPStatus.BAD_REQUEST, f"no folder at {folder}: copy the path from File Explorer's "
                                                           "address bar")
                store.add_folder(folder)
                started = job.start() if self._body().get("scan") else False
                return HTTPStatus.OK, {"folders": store.folders(), "scanning": started}
            if path == "/api/folders/remove":
                if not store.remove_folder(str(self._body().get("path", ""))):
                    raise ApiError(HTTPStatus.NOT_FOUND, "that folder is not registered")
                return HTTPStatus.OK, {"folders": store.folders()}
            if path == "/api/folders/browse":
                self._body()
                if not self._can_browse():
                    raise ApiError(HTTPStatus.FORBIDDEN, "the folder picker opens on the computer running the workspace, "
                                                         "so it works only there: paste the path instead")
                return HTTPStatus.OK, {"path": pick_folder()}
            if path == "/api/rules/reset":
                self._body()
                standards_check.reset_rules(store)
                return HTTPStatus.OK, {"ok": True}
            if path == "/api/rules/import":
                body = self._body()
                return HTTPStatus.OK, {"imported": standards_check.import_rules(store, body, str(body.get("by", "")))}
            match = re.fullmatch(r"/api/rules/([a-z-]+)", path)
            if match:
                body = self._body()
                params = body.get("params")
                if params is not None and not isinstance(params, dict):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "params must be an object")
                return HTTPStatus.OK, standards_check.set_rule(
                    store, match.group(1), body.get("enabled"), body.get("severity"), params, str(body.get("by", "")))
            if path == "/api/exceptions":
                body = self._body()
                model_id, key, rule_id = int(body["model_id"]), str(body["object_key"]), str(body["rule_id"])
                if body.get("remove"):
                    return HTTPStatus.OK, {"removed": standards_check.withdraw(store, model_id, key, rule_id)}
                standards_check.accept(store, model_id, key, rule_id, str(body.get("reason", "")), str(body.get("by", "")))
                return HTTPStatus.OK, {"ok": True}
            match = re.fullmatch(r"/api/versions/(\d+)/(comments|review)", path)
            if match:
                body, version_id = self._body(), int(match.group(1))
                if match.group(2) == "comments":
                    return HTTPStatus.OK, store.add_comment(version_id, str(body.get("body", "")), str(body.get("by", "")),
                                                            str(body.get("object_key", "")))
                return HTTPStatus.OK, store.set_review(version_id, str(body.get("status", "")), str(body.get("by", "")),
                                                       str(body.get("note", "")))
            match = re.fullmatch(r"/api/models/(\d+)", path)
            if match:
                body = self._body()
                store.describe_model(int(match.group(1)), *(None if body.get(k) is None else str(body[k])
                                                             for k in ("owner", "team", "notes")))
                return HTTPStatus.OK, {"ok": True}
            raise ApiError(HTTPStatus.NOT_FOUND, "not found")

        def log_message(self, fmt, *args):
            if not quiet:
                sys.stderr.write(f"  {self.command} {self.path.split('?', 1)[0]} -> {args[1] if len(args) > 1 else ''}\n")

    return Handler


def make_server(store: Store, host: str = "127.0.0.1", port: int = 8766, quiet: bool = False, allow_hosts=(),
                read_only: bool = False, job=None, auth: Optional[AuthConfig] = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), None)
    bound = server.server_address[1]
    names = {f"{h}:{bound}" for h in LOOPBACK} | {f"{host}:{bound}".lower()}
    for extra in allow_hosts:
        extra = extra.strip().lower()
        names |= {extra} if ":" in extra else {extra, f"{extra}:{bound}", f"{extra}:80", f"{extra}:443"}
    server.RequestHandlerClass = make_handler(store, names, read_only, quiet, job, auth)
    return server


def serve(store: Store, host: str = "127.0.0.1", port: int = 8766, open_browser: bool = True, quiet: bool = False,
          allow_hosts=(), read_only: bool = False, scan_every: int = 0, auth: Optional[AuthConfig] = None) -> int:
    auth = auth or AuthConfig()
    if host not in LOOPBACK and auth.mode == "none" and not read_only:
        print(f"Refusing to listen on {host} without sign-in: anyone on the network could change notes and approve\n"
              f"changes. Use --auth local (workspace passwords) or --auth windows (behind IIS), or --read-only.",
              file=sys.stderr)
        return 2
    if auth.mode == "local" and not A.accounts(store):
        print("No accounts yet. Create the first admin:\n  daxlin workspace users add NAME --role admin --password",
              file=sys.stderr)
        return 2
    job = ScanJob(store)
    standards_check.ensure_checked(store)  # versions scanned before the standards existed
    try:
        server = make_server(store, host, port, quiet, allow_hosts, read_only, job, auth)
    except OSError as err:
        print(f"Cannot listen on {host}:{port} ({err.strerror or err}). Another program may be using that port: "
              f"try --port {port + 1}.", file=sys.stderr)
        return 1
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{server.server_address[1]}/"
    print(f"Power BI workspace on {url}  (database: {store.location})", file=sys.stderr)
    sign_in = {"none": "no sign-in (this PC only)", "local": "workspace accounts with passwords",
               "iis": "Windows sign-in by IIS (HttpPlatformHandler forwards each user's token)",
               "windows": f"Windows sign-in through the proxy at {', '.join(auth.trusted_proxies)} "
                          f"(header {auth.user_header})"}[auth.mode]
    print(f"  sign-in: {sign_in}", file=sys.stderr)
    if auth.mode == "local" and host not in LOOPBACK and not auth.secure_cookies:
        print("  warning: passwords travel in clear text unless a proxy in front adds HTTPS; then add --secure-cookies",
              file=sys.stderr)
    if scan_every:
        stop = threading.Event()

        def every():
            while not stop.wait(scan_every * 60):
                job.start()

        threading.Thread(target=every, daemon=True).start()
        print(f"  scanning the registered folders every {scan_every} minutes", file=sys.stderr)
    print("  Ctrl+C to stop.", file=sys.stderr)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
