"""daxlin workspace: the team catalogue from the command line.

    daxlin workspace add-folder "C:/Users/me/Contoso/BI - Reports"
    daxlin workspace scan                    # every registered folder; run it nightly
    daxlin workspace serve                   # http://127.0.0.1:8766
    daxlin workspace models
    daxlin workspace search "Total Sales"    # --dax to look inside DAX too
    daxlin workspace cleanup                 # what the team marked to remove

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
    sub.add_parser("models", help="list models with their unused measures")
    p = sub.add_parser("search", help="find a measure or column in every model")
    p.add_argument("query")
    p.add_argument("--dax", action="store_true", help="look inside DAX expressions too")
    p = sub.add_parser("cleanup", help="what the team marked to remove (or --keep)")
    p.add_argument("--keep", action="store_true")
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
                     read_only=args.read_only)
    elif args.command == "models":
        rows = [(m["name"] + (" (missing)" if m["missing"] else ""), "report" if m["kind"] == "report" else "model",
                 f"v{m['version']}" if m["version"] else "", m["measures"] or 0, m["visuals"] or 0,
                 m["unused_measures"] or 0, m["status"] or "", m["owner"]) for m in store.models()]
        print(_table(rows, ["model", "kind", "ver", "measures", "visuals", "unused", "status", "owner"])
              or "no models yet: daxlin workspace scan")
    elif args.command == "search":
        rows = [(r["name"] if r["kind"] == "measure" else f"{r['table_name']}[{r['name']}]", r["kind"], r["model_name"],
                 STATUS.get(r["usage_status"], r["usage_status"]) if r["in_model"] else "used by this report",
                 r["decision"]) for r in store.search(args.query, in_dax=args.dax)]
        print(_table(rows, ["name", "kind", "model", "usage", "decision"]) or f"nothing called {args.query!r}")
    elif args.command == "cleanup":
        rows = [(r["name"] or "(no longer in the model)", r["model_name"], STATUS.get(r["usage_status"] or "", ""),
                 r["updated_by"], r["note"][:60]) for r in store.decisions("keep" if args.keep else "remove")]
        print(_table(rows, ["name", "model", "usage now", "by", "note"]) or "nothing marked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
