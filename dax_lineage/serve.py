"""Serve the explorer on this machine only.

    daxlin serve                 # http://127.0.0.1:8765, opens the browser
    daxlin serve --port 9000 --no-open

The server hands out one page and nothing else: the self-contained explorer.
Your files are never sent to it. They are read by the page, in the browser,
exactly as on the public site; the server cannot receive them because it
answers only GET and HEAD for "/".

Hardening, for a server that only ever needs to serve one file:
- binds to 127.0.0.1 unless told otherwise (and says so loudly when it is)
- refuses requests whose Host header is not this server, which defeats DNS
  rebinding (a web page in another tab cannot reach it under its own name)
- sends a Content-Security-Policy that blocks every network request from the
  page, plus no-referrer, nosniff, no framing, no caching
"""

from __future__ import annotations

import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .offline import CSP, packaged

HEADERS = {
    "Content-Security-Policy": CSP + "; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), usb=(), payment=(), clipboard-read=()",
    "Cache-Control": "no-store",
}
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def make_handler(page: bytes, allowed_hosts: set, quiet: bool):
    class Handler(BaseHTTPRequestHandler):
        server_version = "daxlin"
        sys_version = ""

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            return host in allowed_hosts

        def _send(self, status: int, body: bytes = b"", content_type: str = "text/plain; charset=utf-8"):
            self.send_response(status)
            for name, value in HEADERS.items():
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            if not self._host_ok():
                self._send(HTTPStatus.MISDIRECTED_REQUEST, b"unknown host\n")
            elif self.path.split("?", 1)[0] in ("/", "/index.html"):
                self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            else:
                self._send(HTTPStatus.NOT_FOUND, b"not found\n")

        do_HEAD = do_GET

        def _refuse(self):
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"this server only serves the page\n")

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

        def log_message(self, fmt, *args):
            if not quiet:
                sys.stderr.write(f"  {self.command} {self.path.split('?', 1)[0]} -> {args[1] if len(args) > 1 else ''}\n")

    return Handler


def make_server(host: str = "127.0.0.1", port: int = 8765, quiet: bool = False, allow_hosts=()) -> ThreadingHTTPServer:
    page = packaged().encode("utf-8")
    server = ThreadingHTTPServer((host, port), None)
    bound = server.server_address[1]
    names = {f"{h}:{bound}" for h in LOOPBACK} | {f"{host}:{bound}".lower()}
    # names the page is reached under behind a proxy or in a container, e.g. lineage.corp or lineage.corp:443
    for extra in allow_hosts:
        extra = extra.strip().lower()
        names |= {extra} if ":" in extra else {extra, f"{extra}:{bound}", f"{extra}:80", f"{extra}:443"}
    server.RequestHandlerClass = make_handler(page, names, quiet)
    return server


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True, quiet: bool = False,
          allow_hosts=()) -> int:
    try:
        server = make_server(host, port, quiet, allow_hosts)
    except OSError as err:
        print(f"Cannot listen on {host}:{port} ({err.strerror or err}). Another program, or another\n"
              f"`daxlin serve`, may be using that port: try --port {port + 1}.", file=sys.stderr)
        return 1
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{server.server_address[1]}/"
    print(f"DAX lineage explorer on {url}", file=sys.stderr)
    if host not in LOOPBACK:
        print(f"  warning: listening on {host}, so other machines can open the page too. Files you drop are\n"
              f"  still read only in your own browser, but prefer 127.0.0.1 unless you mean to share it.",
              file=sys.stderr)
    print("  files you open are read in the browser and never sent to this server. Ctrl+C to stop.", file=sys.stderr)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
