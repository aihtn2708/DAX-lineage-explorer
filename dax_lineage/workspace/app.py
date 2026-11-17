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
from urllib.parse import parse_qs, urlsplit

from ..serve import LOOPBACK
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


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def make_handler(store: Store, allowed_hosts: set, read_only: bool, quiet: bool, job=None):
    job = job or ScanJob(store)

    class Handler(BaseHTTPRequestHandler):
        server_version = "daxlin-workspace"
        sys_version = ""

        # -- plumbing -----------------------------------------------------

        def _send(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            for name, value in HEADERS.items():
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload):
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

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
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise ApiError(HTTPStatus.BAD_REQUEST, "not valid JSON") from None
            if not isinstance(data, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "send a JSON object")
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
            self._api(lambda: self._get(parts.path, parse_qs(parts.query)))

        do_HEAD = do_GET

        def do_POST(self):
            if not self._host_ok():
                return self._send(HTTPStatus.MISDIRECTED_REQUEST, b"unknown host\n", "text/plain; charset=utf-8")
            if not self._origin_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "request from another site refused"})
            if read_only:
                return self._json(HTTPStatus.FORBIDDEN, {"error": "this workspace is read-only"})
            self._api(lambda: self._post(urlsplit(self.path).path))

        def _refuse(self):
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"not allowed\n", "text/plain; charset=utf-8")

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

        def _api(self, call):
            try:
                status, payload = call()
            except ApiError as err:
                status, payload = err.status, {"error": str(err)}
            except KeyError as err:
                message = str(err).strip("'\"")
                status = HTTPStatus.NOT_FOUND if message.startswith("no such") else HTTPStatus.BAD_REQUEST
                payload = {"error": message}
            except ValueError as err:
                status, payload = HTTPStatus.BAD_REQUEST, {"error": str(err)}
            self._json(status, payload)

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
            if path == "/api/reviews":
                status = one("status", "pending")
                if status not in REVIEW_STATES + ("all",):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "unknown review status")
                rows = store.reviews(status)
                for row in rows:
                    row["changes"] = compare(store, row["id"])["counts"]
                return HTTPStatus.OK, rows
            match = re.fullmatch(r"/api/versions/(\d+)", path)
            if match:
                against = one("against")
                if against and not against.isdigit():
                    raise ApiError(HTTPStatus.BAD_REQUEST, "against must be a version id")
                review = compare(store, int(match.group(1)), int(against) if against else None)
                review["comments"] = store.comments(int(match.group(1)))
                review["versions"] = store.versions_of(review["version"]["model_id"])
                return HTTPStatus.OK, review
            match = re.fullmatch(r"/api/(models|objects)/(\d+)", path)
            if match:
                found = (store.model if match.group(1) == "models" else store.object)(int(match.group(2)))
                if found is None:
                    raise ApiError(HTTPStatus.NOT_FOUND, "not found")
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
                read_only: bool = False, job=None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), None)
    bound = server.server_address[1]
    names = {f"{h}:{bound}" for h in LOOPBACK} | {f"{host}:{bound}".lower()}
    for extra in allow_hosts:
        extra = extra.strip().lower()
        names |= {extra} if ":" in extra else {extra, f"{extra}:{bound}", f"{extra}:80", f"{extra}:443"}
    server.RequestHandlerClass = make_handler(store, names, read_only, quiet, job)
    return server


def serve(store: Store, host: str = "127.0.0.1", port: int = 8766, open_browser: bool = True, quiet: bool = False,
          allow_hosts=(), read_only: bool = False, scan_every: int = 0) -> int:
    job = ScanJob(store)
    try:
        server = make_server(store, host, port, quiet, allow_hosts, read_only, job)
    except OSError as err:
        print(f"Cannot listen on {host}:{port} ({err.strerror or err}). Another program may be using that port: "
              f"try --port {port + 1}.", file=sys.stderr)
        return 1
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{server.server_address[1]}/"
    print(f"Power BI workspace on {url}  (database: {store.path})", file=sys.stderr)
    if host not in LOOPBACK:
        print(f"  warning: listening on {host}. There are no sign-ins yet, so anyone who can reach this address\n"
              f"  can read the catalogue{'' if read_only else ' and add notes'}. Put it behind a proxy that signs "
              f"people in, or use --read-only.", file=sys.stderr)
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
