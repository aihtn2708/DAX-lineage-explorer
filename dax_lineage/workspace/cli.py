"""daxlin workspace: the team catalogue from the command line.

    daxlin workspace add-folder "C:/Users/me/Contoso/BI - Reports"
    daxlin workspace scan                    # every registered folder; run it nightly
    daxlin workspace serve                   # http://127.0.0.1:8766
    daxlin workspace models
    daxlin workspace search "Total Sales"    # --dax to look inside DAX too
    daxlin workspace cleanup                 # what the team marked to remove
    daxlin workspace check                   # standards findings; exits 1 when there are errors
    daxlin workspace rules                   # the rule set: set, export, import, reset
    daxlin workspace review                  # versions waiting for review
    daxlin workspace changes "Sales"         # what changed in its latest version
    daxlin workspace changes --since 1d      # everything saved since yesterday, as text

Every command takes --db PATH (default: pbi-workspace.db here, or $DAXLIN_WORKSPACE_DB).
"""

from __future__ import annotations

import argparse
import sys

from .store import Store, default_db_path

STATUS = {"visual": "in visuals", "indirect": "through measures", "structural": "model needs it",
          "deadChain": "unused (dead chain)", "unused": "unused", "": ""}


def _table(rows: list, columns: list) -> str:
    if not rows:
        return ""
    widths = [max(len(title), *(len(str(r[i])) for r in rows)) for i, title in enumerate(columns)]
    line = lambda cells: "  ".join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip()
    return "\n".join([line(columns), line(["-" * w for w in widths])] + [line(r) for r in rows])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="daxlin workspace",
                                     description="A catalogue of every Power BI model your team keeps, on your own machine.")
    parser.add_argument("--db", default=None, help="the workspace database (default: pbi-workspace.db, or $DAXLIN_WORKSPACE_DB)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-folder", help="register a folder of .pbix/.pbit files, e.g. a synced SharePoint library")
    p.add_argument("path")
    p = sub.add_parser("remove-folder", help="stop scanning a folder (its models and history stay)")
    p.add_argument("path")
    sub.add_parser("folders", help="list registered folders")
    p = sub.add_parser("scan", help="read every changed file into a new version")
    p.add_argument("paths", nargs="*", help="scan these folders instead of the registered ones")
    p.add_argument("--force", action="store_true", help="read every file again, even unchanged ones")
    p.add_argument("-q", "--quiet", action="store_true", help="print only the summary and failures")
    p = sub.add_parser("serve", help="open the workspace in the browser")
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8766, help="port (default 8766)")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p.add_argument("--read-only", action="store_true", help="nobody can change notes or start a scan")
    p.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                   help="a host name the page is reached under (behind a proxy)")
    p.add_argument("--scan-every", type=int, default=0, metavar="MINUTES",
                   help="also scan the registered folders every MINUTES while serving")
    sub.add_parser("models", help="list models with their unused measures")
    p = sub.add_parser("search", help="find a measure or column in every model")
    p.add_argument("query")
    p.add_argument("--dax", action="store_true", help="look inside DAX expressions too")
    p = sub.add_parser("cleanup", help="what the team marked to remove (or --keep)")
    p.add_argument("--keep", action="store_true")
    p = sub.add_parser("changes", help="what changed in a model's latest version, or in every version since a time")
    p.add_argument("model", nargs="?", help="model name (default: every model)")
    p.add_argument("--version", type=int, help="this version number instead of the latest")
    p.add_argument("--since", help="every version saved in the last N hours or days: 24h, 1d, 7d")
    p = sub.add_parser("check", help="the standards findings in each model's current version; exits 1 on errors")
    p.add_argument("model", nargs="?", help="model name (default: every model)")
    p.add_argument("--min", choices=["error", "warning", "info"], default="info", help="leave out less severe findings")
    p = sub.add_parser("rules", help="list, change, export or import the team's rules")
    p.add_argument("action", nargs="?", default="list", choices=["list", "set", "export", "import", "reset"])
    p.add_argument("target", nargs="?", help="set: the rule id; import: the JSON file")
    switch = p.add_mutually_exclusive_group()
    switch.add_argument("--on", action="store_true")
    switch.add_argument("--off", action="store_true")
    p.add_argument("--severity", choices=["error", "warning", "info"])
    p.add_argument("--param", action="append", default=[], metavar="NAME=VALUE", help="a rule setting, e.g. pattern=^[A-Z]")
    p.add_argument("-o", "--out", help="export: write to this file instead of the screen")
    p = sub.add_parser("review", help="versions waiting for review (or --all)")
    p.add_argument("--all", action="store_true", help="every reviewable version, with its state")
    args = parser.parse_args(argv)

    store = Store(args.db or default_db_path())

    if args.command == "add-folder":
        from pathlib import Path
        if not Path(args.path).expanduser().is_dir():
            raise SystemExit(f"{args.path}: not a folder")
        folder = store.add_folder(args.path)
        print(f"added {folder['path']}\nnext: daxlin workspace scan")
    elif args.command == "remove-folder":
        print("removed" if store.remove_folder(args.path) else f"{args.path} was not registered")
    elif args.command == "folders":
        folders = store.folders()
        print("\n".join(f["path"] for f in folders) if folders else "no folders: daxlin workspace add-folder PATH")
    elif args.command == "scan":
        from .scan import scan_folders
        log = (lambda line: line.startswith(("failed", "error", "folder not")) and print(line)) if args.quiet else print
        summary = scan_folders(store, args.paths or None, force=args.force, log=log)
        if args.quiet:
            print(summary["log"][-1])
        return 1 if summary["failed"] else 0
    elif args.command == "serve":
        from .app import serve
        return serve(store, args.host, args.port, open_browser=not args.no_open, allow_hosts=args.allow_host,
                     read_only=args.read_only, scan_every=max(0, args.scan_every))
    elif args.command == "models":
        from .check import ensure_checked
        ensure_checked(store)
        rows = [(m["name"] + (" (missing)" if m["missing"] else ""), "report" if m["kind"] == "report" else "model",
                 f"v{m['version']}" if m["version"] else "", m["measures"] or 0, m["visuals"] or 0,
                 m["unused_measures"] or 0, "" if m["score"] is None else m["score"], m["status"] or "", m["owner"])
                for m in store.models()]
        print(_table(rows, ["model", "kind", "ver", "measures", "visuals", "unused", "score", "status", "owner"])
              or "no models yet: daxlin workspace scan")
    elif args.command == "search":
        rows = [(r["name"] if r["kind"] == "measure" else f"{r['table_name']}[{r['name']}]", r["kind"], r["model_name"],
                 STATUS.get(r["usage_status"], r["usage_status"]) if r["in_model"] else "used by this report",
                 r["decision"]) for r in store.search(args.query, in_dax=args.dax)]
        print(_table(rows, ["name", "kind", "model", "usage", "decision"]) or f"nothing called {args.query!r}")
    elif args.command == "changes":
        return _changes(store, args)
    elif args.command == "check":
        return _check(store, args)
    elif args.command == "rules":
        return _rules(store, args)
    elif args.command == "review":
        from .diff import compare
        rows = []
        for r in store.reviews("all" if args.all else "pending"):
            c = compare(store, r["id"])["counts"]
            rows.append((r["model_name"], f"v{r['number']}", r["file_modified_at"][:16].replace("T", " "),
                         c.get("total", 0), c.get("dax", 0), c.get("broken", 0), r["review"].replace("_", " "),
                         r["reviewer"]))
        print(_table(rows, ["model", "ver", "saved (UTC)", "changes", "dax", "broken", "review", "by"])
              or "nothing waiting for review")
    elif args.command == "cleanup":
        rows = [(r["name"] or "(no longer in the model)", r["model_name"], STATUS.get(r["usage_status"] or "", ""),
                 r["updated_by"], r["note"][:60]) for r in store.decisions("keep" if args.keep else "remove")]
        print(_table(rows, ["name", "model", "usage now", "by", "note"]) or "nothing marked")
    return 0


def _check(store: Store, args) -> int:
    from .check import ensure_checked, version_check
    ensure_checked(store)
    order = ["error", "warning", "info"]
    keep = order[:order.index(args.min) + 1]
    models = [m for m in store.models() if not m["missing"] and m["version_id"]]
    if args.model:
        models = [m for m in models if m["name"].casefold() == args.model.casefold()]
        if not models:
            raise SystemExit(f"no model called {args.model!r}: see `daxlin workspace models`")
    errors, blocks = 0, []
    for m in models:
        result = version_check(store, m["version_id"])
        if result is None:
            blocks.append(f"{m['name']} v{m['version']}: not checked (no model read from this file)")
            continue
        found = [f for f in result["findings"] if not f["accepted"] and f["severity"] in keep]
        errors += len([f for f in found if f["severity"] == "error"])
        head = (f"{m['name']} v{m['version']}: score {result['score']}, {result['errors']} errors, "
                f"{result['warnings']} warnings, {result['infos']} notes"
                + (f", {result['accepted']} accepted" if result["accepted"] else ""))
        rows = [(f["severity"], f["rule_id"], f["label"], f["message"][:70]) for f in found]
        blocks.append(head + ("\n" + _table(rows, ["severity", "rule", "object", "finding"]) if rows else ""))
    print("\n\n".join(blocks) if blocks else "no models")
    return 1 if errors else 0


def _rules(store: Store, args) -> int:
    import json
    from .check import export_rules, import_rules, reset_rules, rule_set, set_rule
    if args.action == "list":
        rows = [(r["id"], "on" if r["enabled"] else "off", r["severity"],
                 ", ".join(f"{k}={v}" for k, v in r["params"].items() if v != ""), r["title"]) for r in rule_set(store)]
        print(_table(rows, ["rule", "", "severity", "settings", "what it checks"]))
    elif args.action == "set":
        if not args.target:
            raise SystemExit("rules set needs a rule id: see `daxlin workspace rules`")
        params = {}
        for item in args.param:
            if "=" not in item:
                raise SystemExit(f"--param takes NAME=VALUE, not {item!r}")
            key, value = item.split("=", 1)
            params[key.strip()] = value
        try:
            rule = set_rule(store, args.target, True if args.on else False if args.off else None, args.severity,
                            params or None, by="command line")
        except (KeyError, ValueError) as err:
            raise SystemExit(str(err).strip("'\"")) from None
        print(f"{rule['id']}: {'on' if rule['enabled'] else 'off'}, {rule['severity']}, {rule['params'] or ''}\n"
              "every model's current version has been checked again")
    elif args.action == "export":
        text = json.dumps(export_rules(store), indent=2)
        if args.out:
            from pathlib import Path
            Path(args.out).write_text(text + "\n", encoding="utf-8")
            print(f"wrote {args.out}")
        else:
            print(text)
    elif args.action == "import":
        if not args.target:
            raise SystemExit("rules import needs a JSON file made by `rules export`")
        from pathlib import Path
        try:
            count = import_rules(store, json.loads(Path(args.target).read_text(encoding="utf-8")), by="command line")
        except (ValueError, KeyError) as err:
            raise SystemExit(f"{args.target}: {err}") from None
        print(f"imported {count} rules; every model's current version has been checked again")
    elif args.action == "reset":
        reset_rules(store)
        print("every rule is back to its default")
    return 0


def _since(text: str) -> str:
    import re
    from datetime import datetime, timedelta, timezone
    match = re.fullmatch(r"(\d+)\s*([hd])", (text or "").strip().lower())
    if not match:
        raise SystemExit("--since takes hours or days, like 24h or 7d")
    amount = int(match.group(1))
    delta = timedelta(hours=amount) if match.group(2) == "h" else timedelta(days=amount)
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def _changes(store: Store, args) -> int:
    from .diff import as_text, compare
    models = store.models()
    if args.model:
        models = [m for m in models if m["name"].casefold() == args.model.casefold()]
        if not models:
            raise SystemExit(f"no model called {args.model!r}: see `daxlin workspace models`")
    since = _since(args.since) if args.since else None
    blocks = []
    for m in models:
        versions = store.versions_of(m["id"])
        if args.version:
            chosen = [v for v in versions if v["number"] == args.version]
            if not chosen:
                raise SystemExit(f"{m['name']} has no version {args.version}")
        elif since:
            with store.connect() as con:
                chosen = [dict(r) for r in con.execute(
                    "SELECT id FROM model_version WHERE model_id = ? AND scanned_at >= ? ORDER BY number",
                    (m["id"], since))]
        else:
            chosen = versions[:1]
        blocks += [as_text(compare(store, v["id"])) for v in chosen]
    print("\n\n".join(blocks) if blocks else "no new versions in that time")
    return 0


if __name__ == "__main__":
    sys.exit(main())
