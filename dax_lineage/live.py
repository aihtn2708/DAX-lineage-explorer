"""Live lineage: follow the measure you are editing in Power BI Desktop, on a second screen.

    daxlin live                       # http://127.0.0.1:8770, opens the browser
    daxlin live --report Sales.pbix   # visuals from this saved file when Desktop's file is not known

A watcher started from Desktop's External Tools ribbon (live_tool/PowerBILive.ps1) reads the
model open in Desktop about once a second and posts it here whenever something changed. This
server works out what changed, builds the lineage with the same parser as the explorer, and
pushes the result to the page with server-sent events.

Desktop only changes the model when an edit is committed (Enter, or the tick in the formula
bar), so the page follows commits, not keystrokes.

Same hardening as ``daxlin serve``: it binds to 127.0.0.1, refuses other Host headers, sends a
strict Content-Security-Policy, and only takes posts that carry the token it writes to
~/.dax_lineage/live.json, which only this user can read.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .graph import build_graph, column_id, measure_id
from .load import catalogue_from_rows

DEFAULT_PORT = 8770
WEB = Path(__file__).parent / "web"
TOKEN_FILE = Path.home() / ".dax_lineage" / "live.json"
MAX_BODY = 64 * 1024 * 1024
ERROR_STATES = {"SemanticError", "EvaluationError", "DependencyError", "SyntaxError", "Incomplete"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}
STATIC = {"/": ("live.html", "text/html; charset=utf-8"), "/live.js": ("live.js", "text/javascript; charset=utf-8"),
          "/live.css": ("live.css", "text/css; charset=utf-8")}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(text) -> str:
    return "\n".join(line.rstrip() for line in str(text or "").replace("\r\n", "\n").strip().split("\n"))


def is_error(state: str) -> bool:
    return (state or "") in ERROR_STATES


# -- one model open in Desktop ------------------------------------------------------------------

class ModelState:
    """The latest snapshot of one model, its lineage, and what the saved file's visuals read."""

    def __init__(self, source: str, snapshot: dict, usages: list, report_note: str):
        self.source = source
        self.snapshot = snapshot
        self.name = snapshot.get("name") or (Path(snapshot["file"]).stem if snapshot.get("file") else
                                             f"Model at {source}")
        self.file = snapshot.get("file") or ""
        self.report_note = report_note
        self.received = now()
        self.objects = objects_of(snapshot)
        found = {k: snapshot.get(k) or [] for k in ("measures", "tables", "columns", "relationships")}
        self.lineage = build_graph(catalogue_from_rows(found), usages=usages) if found["measures"] else None
        self.has_report = bool(usages)

    def node_for(self, obj: dict) -> str:
        return measure_id(obj["name"]) if obj["kind"] == "measure" else column_id(obj["table"], obj["name"])

    def summary(self) -> dict:
        stats = self.lineage.stats() if self.lineage else {}
        return {"source": self.source, "name": self.name, "file": self.file, "received": self.received,
                "measures": sum(1 for m in self.snapshot.get("measures") or [] if m.get("Name")),
                "columns": len(self.snapshot.get("columns") or []),
                "visuals": stats.get("report", {}).get("visuals", 0) if self.has_report else None,
                "reportNote": self.report_note,
                "broken": sorted(o["name"] for o in self.objects.values() if is_error(o["state"]))}


def objects_of(snapshot: dict) -> dict:
    """Measures and calculated columns, keyed by lineage tag (or name when a model has none)."""
    out = {}
    for row in snapshot.get("measures") or []:
        name = str(row.get("Name") or "").strip()
        if not name:
            continue
        key = row.get("LineageTag") or f"measure:{name.casefold()}"
        out[key] = {"key": key, "kind": "measure", "name": name, "table": row.get("Table") or "",
                    "expression": _norm(row.get("Expression")), "state": row.get("State") or "",
                    "error": row.get("ErrorMessage") or "", "modified": str(row.get("ModifiedTime") or ""),
                    "format": row.get("FormatString") or "", "description": row.get("Description") or "",
                    "folder": row.get("DisplayFolder") or "", "hidden": bool(row.get("IsHidden"))}
    for row in snapshot.get("columns") or []:
        expression = _norm(row.get("Expression"))
        name = str(row.get("ExplicitName") or row.get("Name") or "").strip()
        if not expression or not name:
            continue  # only calculated columns are edited as DAX
        key = row.get("LineageTag") or f"column:{str(row.get('Table') or '').casefold()}|{name.casefold()}"
        out[key] = {"key": key, "kind": "column", "name": name, "table": row.get("Table") or "",
                    "expression": expression, "state": row.get("State") or "", "error": row.get("ErrorMessage") or "",
                    "modified": str(row.get("ModifiedTime") or ""), "format": row.get("FormatString") or "",
                    "description": row.get("Description") or "", "folder": row.get("DisplayFolder") or "",
                    "hidden": bool(row.get("IsHidden"))}
    return out


def changes_between(before: dict, after: dict) -> list:
    """What changed between two snapshots' objects, one entry per object."""
    out = []
    for key, obj in after.items():
        old = before.get(key)
        if old is None:
            out.append({"change": "added", **_brief(obj)})
            continue
        parts = []
        if old["name"] != obj["name"]:
            parts.append("renamed")
        if old["expression"] != obj["expression"]:
            parts.append("edited")
        if old["table"] != obj["table"]:
            parts.append("moved")
        for field, label in (("format", "format"), ("description", "description"), ("folder", "folder"),
                             ("hidden", "hidden")):
            if old[field] != obj[field]:
                parts.append(label)
        if not parts and is_error(old["state"]) != is_error(obj["state"]):
            parts.append("broke" if is_error(obj["state"]) else "fixed")
        if parts:
            out.append({"change": parts[0], "parts": parts, "oldName": old["name"], **_brief(obj)})
    for key, old in before.items():
        if key not in after:
            out.append({"change": "removed", **_brief(old)})
    return out


def _brief(obj: dict) -> dict:
    return {"key": obj["key"], "kind": obj["kind"], "name": obj["name"], "table": obj["table"],
            "modified": obj["modified"]}


PRIORITY = {"edited": 0, "renamed": 0, "added": 0, "moved": 1, "removed": 2, "format": 3, "description": 3,
            "folder": 3, "hidden": 3, "broke": 4, "fixed": 4}


def pick_focus(changes: list) -> Optional[dict]:
    """The object the person most likely just edited: a DAX change before a property change, newest first."""
    if not changes:
        return None
    best = min(PRIORITY.get(c["change"], 5) for c in changes)
    top = [c for c in changes if PRIORITY.get(c["change"], 5) == best]
    return max(top, key=lambda c: c["modified"] or "")


# -- what an object touches ----------------------------------------------------------------------

def _levels(lineage, start: str, step) -> list:
    """Breadth-first: each reachable node once, with how many steps away it is."""
    seen, frontier, out = {start}, [start], []
    level = 0
    while frontier:
        level += 1
        nxt = []
        for current in frontier:
            for node_id in step(current):
                if node_id in seen or node_id not in lineage.nodes:
                    continue
                seen.add(node_id)
                nxt.append(node_id)
                out.append((node_id, level, current))
        frontier = nxt
    return out


def _node(lineage, node_id: str) -> dict:
    n = lineage.nodes[node_id]
    return {"id": node_id, "kind": n.kind, "name": n.name, "table": n.table, "exists": n.exists,
            "state": n.state, "error": n.error_message}


def analyse(model: ModelState, node_id: str, previous: Optional[ModelState] = None) -> dict:
    """Upstream, downstream, visuals and warnings for one measure or column."""
    lineage = model.lineage
    if lineage is None or node_id not in lineage.nodes:
        return {"found": False}
    node = lineage.nodes[node_id]
    upstream = [{**_node(lineage, i), "level": lvl}
                for i, lvl, _ in _levels(lineage, node_id, lineage.dependencies)
                if lineage.nodes[i].kind != "visual"]
    downstream, visuals = [], []
    for i, lvl, via in _levels(lineage, node_id, lineage.dependents):
        n = lineage.nodes[i]
        if n.kind == "visual":
            visuals.append({"id": i, "report": n.report, "page": n.page, "pageOrder": n.page_order,
                            "visual": n.name, "type": n.visual_type, "direct": via == node_id,
                            "via": "" if via == node_id else lineage.nodes[via].name})
        else:
            downstream.append({**_node(lineage, i), "level": lvl,
                               "via": "" if via == node_id else lineage.nodes[via].name})
    before_errors = set()
    if previous and previous.lineage:
        before_errors = {i for i, n in previous.lineage.nodes.items() if is_error(n.state)}
    for d in downstream:
        d["newError"] = is_error(d["state"]) and d["id"] not in before_errors
    visuals.sort(key=lambda v: (v["report"], v["pageOrder"], v["page"], not v["direct"], v["visual"]))

    warnings = []
    if is_error(node.state):
        warnings.append({"level": "error", "text": f"This {node.kind} does not work: {node.error_message or node.state}"})
    missing = sorted({u["name"] for u in upstream if not u["exists"] and u["level"] == 1} | set(node.unresolved))
    if missing:
        warnings.append({"level": "error", "text": "It reads something the model does not have: "
                         + ", ".join(f"[{m}]" for m in missing)})
    if any(node_id in cycle for cycle in lineage.cycles):
        warnings.append({"level": "error", "text": "It is part of a loop: it ends up reading itself."})
    new_errors = [d for d in downstream if d["newError"]]
    old_errors = [d for d in downstream if is_error(d["state"]) and not d["newError"]]
    if new_errors:
        warnings.append({"level": "error", "text": f"{len(new_errors)} {'measure' if len(new_errors) == 1 else 'measures'}"
                         " that read this stopped working: " + ", ".join(d["name"] for d in new_errors[:6])
                         + ("…" if len(new_errors) > 6 else "")})
    if old_errors:
        warnings.append({"level": "warning", "text": f"{len(old_errors)} that read this were already broken: "
                         + ", ".join(d["name"] for d in old_errors[:6])})
    if visuals and is_error(node.state):
        warnings.append({"level": "error", "text": f"{len(visuals)} visuals in the saved report show it, so they "
                         "will show an error too."})
    if not visuals and not downstream and model.has_report and node.kind == "measure":
        warnings.append({"level": "info", "text": "Nothing reads it: no measure, and no visual in the saved report."})
    return {"found": True, "id": node_id, "kind": node.kind, "name": node.name, "table": node.table,
            "expression": node.expression, "state": node.state, "error": node.error_message,
            "format": node.format_string, "description": node.description,
            "upstream": upstream, "downstream": downstream, "visuals": visuals, "warnings": warnings,
            "hasReport": model.has_report}


# -- every model the watchers report ---------------------------------------------------------------

class Live:
    """What the page shows: every model a watcher reports, and the edits seen so far."""

    def __init__(self, report: Optional[str] = None, history: int = 100):
        self.report = report
        self.models: dict = {}
        self.report_for: dict = {}
        self.events: deque = deque(maxlen=history)
        self.seq = 0
        self.lock = threading.Condition()
        self._usage_cache: dict = {}

    # saved report files, read again only when they change on disk
    def _usages(self, path: str) -> tuple:
        if not path:
            return [], "No saved report file is known for this model, so visuals are not shown."
        p = Path(path)
        try:
            stamp = p.stat().st_mtime_ns
        except OSError:
            return [], f"The saved file {p.name} was not found."
        cached = self._usage_cache.get(path)
        if cached and cached[0] == stamp:
            return cached[1], cached[2]
        from .pbi_report import read_report
        try:
            result = read_report(p)
            usages, note = (result.usages if result else []), ""
            if not usages:
                note = f"{p.name} has no report pages to read visuals from."
        except Exception as err:  # a file Desktop is writing at this moment, or not a report
            usages, note = [], f"{p.name} could not be read ({err})."
        else:
            note = note or f"Visuals from {p.name} as last saved."
        self._usage_cache[path] = (stamp, usages, note)
        return usages, note

    def set_report(self, source: str, path: str) -> dict:
        path = str(path or "").strip().strip('"')
        if path and not Path(path).exists():
            raise ValueError(f"no file at {path}")
        with self.lock:
            self.report_for[source] = path
            model = self.models.get(source)
        if model:
            self.receive({**model.snapshot, "source": source}, quiet=True)
        return self.state()

    def receive(self, snapshot: dict, quiet: bool = False) -> Optional[dict]:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("measures"), list):
            raise ValueError("a snapshot needs a list of measures")
        source = str(snapshot.get("source") or "desktop")
        path = self.report_for.get(source) or snapshot.get("file") or self.report or ""
        usages, note = self._usages(path)
        with self.lock:
            previous = self.models.get(source)
        model = ModelState(source, snapshot, usages, note)
        changes = changes_between(previous.objects, model.objects) if previous else []
        with self.lock:
            self.models[source] = model
            if previous is not None and not changes and not quiet:
                return None
            self.seq += 1
            event = {"seq": self.seq, "at": now(), "source": source, "model": model.summary()}
            if previous is None:
                event["kind"] = "connected"
            elif quiet:
                event["kind"] = "report"
            else:
                event["kind"] = "change"
                event["changes"] = changes
                focus = pick_focus(changes)
                if focus:
                    event["focus"] = self._focus(model, previous, focus)
            self.events.append(event)
            self.lock.notify_all()
            return event

    def _focus(self, model: ModelState, previous: ModelState, change: dict) -> dict:
        before = previous.objects.get(change["key"])
        after = model.objects.get(change["key"])
        if after is None:  # removed: what read it, from the model as it was
            node_id = previous.node_for(before)
            out = analyse(previous, node_id)
            out["warnings"] = [w for w in out.get("warnings", []) if w["level"] != "info"]
            readers = len(out.get("downstream", [])) + len(out.get("visuals", []))
            if readers:
                out["warnings"].insert(0, {"level": "error", "text": f"It was read by {len(out['downstream'])} "
                                           f"measures and {len(out['visuals'])} visuals, which now have nothing to read."})
        else:
            out = analyse(model, model.node_for(after), previous)
        out.update({"change": change["change"], "parts": change.get("parts", [change["change"]]),
                    "key": change["key"], "before": before["expression"] if before else "",
                    "after": after["expression"] if after else "", "oldName": before["name"] if before else ""})
        if "renamed" in out["parts"] and out.get("visuals"):
            out["warnings"].append({"level": "info", "text": f"Renamed from [{before['name']}]. Desktop updates this "
                                    "report's visuals and measures; other files with a copy of it keep the old name."})
        return out

    def explore(self, source: str, node_id: str) -> dict:
        with self.lock:
            model = self.models.get(source)
        if model is None:
            raise KeyError("no such model")
        return analyse(model, node_id)

    def find(self, source: str, text: str) -> list:
        with self.lock:
            model = self.models.get(source)
        if model is None or model.lineage is None:
            return []
        q = text.strip().casefold()
        hits = [n for n in model.lineage.nodes.values() if n.kind in ("measure", "column") and n.exists
                and q in n.name.casefold()]
        hits.sort(key=lambda n: (n.name.casefold() != q, n.kind != "measure", n.name.casefold()))
        return [{"id": n.id, "kind": n.kind, "name": n.name, "table": n.table} for n in hits[:30]]

    def state(self) -> dict:
        with self.lock:
            return {"models": [m.summary() for m in sorted(self.models.values(), key=lambda m: m.received,
                                                            reverse=True)],
                    "events": list(self.events), "seq": self.seq}

    def wait(self, after: int, timeout: float) -> list:
        with self.lock:
            if self.seq <= after:
                self.lock.wait(timeout)
            return [e for e in self.events if e["seq"] > after]


# -- the server -----------------------------------------------------------------------------------

def make_handler(live: Live, token: str, allowed_hosts: set, quiet: bool):
    class Handler(BaseHTTPRequestHandler):
        server_version = "daxlin-live"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            if not quiet:
                sys.stderr.write("live: " + fmt % args + "\n")

        def _send(self, status, body: bytes, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            for k, v in HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status, data):
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").strip().lower() in allowed_hosts

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._host_ok():
                return self._send(HTTPStatus.MISDIRECTED_REQUEST, b"wrong host", "text/plain")
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path in STATIC:
                name, kind = STATIC[url.path]
                return self._send(HTTPStatus.OK, (WEB / name).read_bytes(), kind)
            if url.path == "/api/hello":
                return self._json(HTTPStatus.OK, {"app": "daxlin-live", "pid": os.getpid()})
            if url.path == "/api/state":
                # the page needs the token to post; other sites cannot read this answer (no CORS, Host checked)
                return self._json(HTTPStatus.OK, {**live.state(), "token": token})
            if url.path == "/api/explore":
                try:
                    return self._json(HTTPStatus.OK, live.explore(q.get("source", ""), q.get("id", "")))
                except KeyError as err:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": str(err)})
            if url.path == "/api/find":
                return self._json(HTTPStatus.OK, live.find(q.get("source", ""), q.get("q", "")))
            if url.path == "/api/events":
                return self._stream(int(q.get("after", "0") or 0))
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _stream(self, after: int):
            self.send_response(HTTPStatus.OK)
            for k, v in HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while not self.server.stopping:
                    events = live.wait(after, 15)
                    if not events:
                        self.wfile.write(b": still here\n\n")
                    for event in events:
                        after = event["seq"]
                        data = json.dumps(event, ensure_ascii=False)
                        self.wfile.write(f"id: {after}\nevent: update\ndata: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return

        def do_POST(self):
            if not self._host_ok():
                return self._send(HTTPStatus.MISDIRECTED_REQUEST, b"wrong host", "text/plain")
            if not secrets.compare_digest(self.headers.get("X-Live-Token") or "", token):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "missing or wrong token"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8-sig") or "{}")
                path = urlparse(self.path).path
                if path == "/api/snapshot":
                    event = live.receive(body)
                    return self._json(HTTPStatus.OK, {"changed": event is not None,
                                                      "seq": event["seq"] if event else live.seq})
                if path == "/api/report":
                    return self._json(HTTPStatus.OK, live.set_report(str(body.get("source", "")), body.get("path", "")))
            except (ValueError, KeyError) as err:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(err)})
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    return Handler


class LiveServer(ThreadingHTTPServer):
    daemon_threads = True
    stopping = False


def make_server(live: Live, port: int = DEFAULT_PORT, token: Optional[str] = None, quiet: bool = True) -> LiveServer:
    token = token or secrets.token_urlsafe(24)
    server = LiveServer(("127.0.0.1", port), None)
    port = server.server_address[1]
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    server.RequestHandlerClass = make_handler(live, token, hosts, quiet)
    server.token = token
    server.live = live
    return server


def write_token_file(port: int, token: str, path: Path = TOKEN_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"port": port, "token": token, "pid": os.getpid(), "url": f"http://127.0.0.1:{port}/"}, f)


def serve(port: int = DEFAULT_PORT, report: Optional[str] = None, open_browser: bool = True,
          quiet: bool = True) -> int:
    live = Live(report)
    try:
        server = make_server(live, port, quiet=quiet)
    except OSError as err:
        print(f"Could not listen on 127.0.0.1:{port} ({err}). Is daxlin live already running?", file=sys.stderr)
        return 1
    port = server.server_address[1]
    write_token_file(port, server.token)
    url = f"http://127.0.0.1:{port}/"
    print(f"Live lineage on {url}")
    print("Start 'Live lineage' from Power BI Desktop's External Tools ribbon. Ctrl+C stops this.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stopping = True
        with live.lock:
            live.lock.notify_all()
        server.server_close()
        try:
            if json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("pid") == os.getpid():
                TOKEN_FILE.unlink()
        except (OSError, ValueError):
            pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(serve())
