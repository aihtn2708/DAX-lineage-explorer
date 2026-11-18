"""daxlin workspace: the team catalogue from the command line.

    daxlin workspace add-folder "C:/Users/me/Contoso/BI - Reports"
    daxlin workspace scan                    # every registered folder; run it nightly
    daxlin workspace serve                   # http://127.0.0.1:8766
    daxlin workspace models
    daxlin workspace search "Total Sales"    # --dax to look inside DAX too
    daxlin workspace cleanup                 # what the team marked to remove
    daxlin workspace check                   # standards findings; exits 1 when there are errors
    daxlin workspace rules                   # the rule set: set, export, import, reset
    daxlin workspace requests                # change requests waiting or approved
    daxlin workspace companion               # the Power BI Desktop companion, to install on each PC
    daxlin workspace review                  # versions waiting for review
    daxlin workspace changes "Sales"         # what changed in its latest version
    daxlin workspace changes --since 1d      # everything saved since yesterday, as text

Every command takes --db (default: pbi-workspace.db here, or $DAXLIN_WORKSPACE_DB): a SQLite
file, or a Postgres URL such as postgresql://workspace@db.corp/powerbi for the team server.

    daxlin workspace users add contoso\\linh --role admin    # accounts and roles
    daxlin workspace serve --auth iis --allow-host bi.corp       # started by IIS, Windows sign-in
    daxlin workspace backup --out workspace.jsonl.gz           # restore it anywhere with `restore`
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
    parser.add_argument("--db", default=None,
                        help="a SQLite file or a postgresql:// URL (default: pbi-workspace.db, or $DAXLIN_WORKSPACE_DB)")
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
    p.add_argument("--auth", choices=["none", "local", "iis", "windows"], default="none",
                   help="sign-in: none (this PC only), local (workspace passwords), iis (Windows sign-in, run by IIS), "
                        "windows (a proxy that signs people in passes the user name in a header)")
    p.add_argument("--trusted-proxy", action="append", default=[], metavar="ADDRESS",
                   help="iis/windows: the address requests come from (default 127.0.0.1 and ::1)")
    p.add_argument("--user-header", default="X-Remote-User", help="windows: the header holding the user name")
    p.add_argument("--default-role", choices=["viewer", "author", "reviewer", "admin"], default="viewer",
                   help="iis/windows: the role of someone signing in for the first time (default viewer)")
    p.add_argument("--allow-self-approval", action="store_true",
                   help="let people approve their own changes (off by default)")
    p.add_argument("--session-hours", type=int, default=12, help="local: how long a sign-in lasts (default 12)")
    p.add_argument("--secure-cookies", action="store_true", help="local: the page is served over HTTPS by a proxy")
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
    p = sub.add_parser("requests", help="change requests: open ones, or --all")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("companion", help="write the Power BI Desktop companion and its installer to a folder")
    p.add_argument("--out", default="powerbi-workspace-companion", help="folder to write (default: powerbi-workspace-companion)")
    p = sub.add_parser("users", help="accounts and roles: list, add, set")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add", "set"])
    p.add_argument("username", nargs="?")
    p.add_argument("--role", choices=["viewer", "author", "reviewer", "admin"])
    p.add_argument("--display-name")
    p.add_argument("--password", action="store_true", help="ask for a password (for --auth local)")
    p.add_argument("--disable", action="store_true")
    p.add_argument("--enable", action="store_true")
    p = sub.add_parser("tokens", help="API tokens, e.g. for the Desktop companion: create, list, revoke")
    p.add_argument("action", choices=["create", "list", "revoke"])
    p.add_argument("target", help="create/list: the user name; revoke: the token's first 12 characters")
    p.add_argument("--name", default="companion", help="create: what the token is for")
    p = sub.add_parser("audit", help="who changed what, newest first")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--user", default="")
    p = sub.add_parser("backup", help="write the whole workspace to one file")
    p.add_argument("--out", required=True, help="the file to write, e.g. workspace.jsonl.gz")
    p = sub.add_parser("restore", help="load a backup into an empty workspace (SQLite or Postgres)")
    p.add_argument("file")
    p.add_argument("--force", action="store_true", help="replace what this workspace holds")
    p = sub.add_parser("metrics", help="the metric glossary: each metric's copies and drift, or candidates")
    p.add_argument("action", nargs="?", default="list", choices=["list", "drift", "candidates"])
    p = sub.add_parser("policies", help="each tier's policy, and the models that do not keep to theirs")
    p = sub.add_parser("tier", help="put a model in a tier: personal, team or certified")
    p.add_argument("model", help="the model's name or id (see `models`)")
    p.add_argument("tier", choices=["personal", "team", "certified"])
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
        from .app import AuthConfig, serve
        auth = AuthConfig(mode=args.auth, trusted_proxies=tuple(args.trusted_proxy or ("127.0.0.1", "::1")),
                          user_header=args.user_header, default_role=args.default_role,
                          allow_self_approval=args.allow_self_approval, session_hours=max(1, args.session_hours),
                          secure_cookies=args.secure_cookies)
        return serve(store, args.host, args.port, open_browser=not args.no_open, allow_hosts=args.allow_host,
                     read_only=args.read_only, scan_every=max(0, args.scan_every), auth=auth)
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
    elif args.command == "requests":
        from .changes import requests
        rows = [(f"#{r['id']}", r["model_name"], r["title"][:60], r["edits"], r["status"], r["created_by"], r["decided_by"])
                for r in requests(store, "all" if args.all else "open")]
        print(_table(rows, ["id", "model", "change", "edits", "status", "proposed by", "decided by"]) or "no open changes")
    elif args.command == "companion":
        return _companion(args.out)
    elif args.command == "metrics":
        from . import metrics as M
        data = M.glossary(store)
        if args.action == "candidates":
            rows = [(c["name"], c["models"], c["versions"], ", ".join(x["model_name"] for x in c["copies"])[:70])
                    for c in data["candidates"]]
            print(_table(rows, ["measure", "models", "different DAX", "in"]) or "no measure is copied across models")
        elif args.action == "drift":
            rows = []
            for m in data["metrics"]:
                for c in M.copies(store, M.get(store, m["id"])):
                    if c["status"] == "drift":
                        rows.append((m["name"], c["model_name"], c["name"], m["owner"]))
            print(_table(rows, ["metric", "model", "measure", "metric owner"]) or "no drift")
        else:
            rows = [(m["name"], m["status"] + (" (source changed)" if m["source"] == "changed" else ""), m["owner"],
                     m["model_name"] or "", m["counts"]["same"], m["counts"]["variant"], m["counts"]["drift"])
                    for m in data["metrics"]]
            print(_table(rows, ["metric", "status", "owner", "defined in", "same", "variants", "drift"])
                  or "no metrics yet: make one from a measure's page, or see `metrics candidates`")
    elif args.command == "policies":
        from . import policy as P
        data = P.overview(store)
        for tier in P.TIERS:
            t = data["tiers"][tier]
            parts = ["every save reviewed" if t["require_review"] else "no review needed"]
            if t["no_new_errors"]:
                parts.append("no new errors")
            if t["max_score_drop"] is not None:
                parts.append(f"score drop at most {t['max_score_drop']}")
            if t["min_score"] is not None:
                parts.append(f"score at least {t['min_score']}")
            if t["rules"]:
                parts.append("stricter rules: " + ", ".join(f"{k} {v.get('severity') or ('on' if v.get('enabled') else 'off')}"
                                                            for k, v in t["rules"].items()))
            print(f"{tier:<10} {'; '.join(parts)}")
        rows = [(m["name"], m["tier"], "" if m["score"] is None else m["score"], "; ".join(m["problems"])[:110])
                for m in data["models"] if not m["ok"]]
        print()
        print(_table(rows, ["model", "tier", "score", "out of policy because"]) or "every model keeps to its tier's policy")
    elif args.command == "tier":
        from .policy import set_tier
        wanted = args.model.strip()
        found = [m for m in store.models() if str(m["id"]) == wanted or m["name"].casefold() == wanted.casefold()]
        if len(found) != 1:
            raise SystemExit(f"no model called {wanted!r}" if not found else f"{wanted!r} matches more than one model; give its id")
        set_tier(store, found[0]["id"], args.tier, "command line")
        _audit(store, "models.tier", found[0]["name"], {"tier": args.tier})
        print(f"{found[0]['name']} is now {args.tier}")
    elif args.command == "users":
        return _users(store, args)
    elif args.command == "tokens":
        from . import auth as A
        if args.action == "create":
            try:
                token = A.create_token(store, args.target, args.name)
            except KeyError as err:
                raise SystemExit(str(err).strip("'\"")) from None
            _audit(store, "tokens.create", args.target, {"name": args.name, "prefix": token[:12]})
            print(f"{token}\nShown once: put it in the companion's companion.config.json (\"token\"), "
                  f"or run install-companion.ps1 -Token {token[:12]}...")
        elif args.action == "list":
            rows = [(t["prefix"], t["name"], t["created_at"][:16], (t["last_used_at"] or "never")[:16])
                    for t in A.tokens(store, args.target)]
            print(_table(rows, ["token", "for", "created", "last used"]) or "no tokens")
        else:
            gone = A.revoke_token(store, args.target)
            if gone:
                _audit(store, "tokens.revoke", args.target)
            print("revoked" if gone else "no such token")
    elif args.command == "audit":
        from . import auth as A
        rows = [(r["at"][:19].replace("T", " "), r["username"], r["action"], r["target"], r["address"],
                 r["detail"][:60]) for r in A.audit_log(store, args.limit, args.user)]
        print(_table(rows, ["when (UTC)", "who", "did", "on", "from", "detail"]) or "nothing yet")
    elif args.command == "backup":
        from .backup import backup
        counts = backup(store, args.out)
        print(f"wrote {args.out}: " + ", ".join(f"{n} {t}" for t, n in counts.items() if n))
    elif args.command == "restore":
        from .backup import restore
        try:
            counts = restore(store, args.file, force=args.force)
        except ValueError as err:
            raise SystemExit(str(err)) from None
        _audit(store, "restore", str(args.file))
        print(f"restored into {store.location}: " + ", ".join(f"{n} {t}" for t, n in counts.items() if n))
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
            _audit(store, "rules.set", args.target, {"on": args.on, "off": args.off, "severity": args.severity,
                                                     "params": params})
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


def _audit(store: Store, action: str, target: str = "", detail=None) -> None:
    """Changes made from the command line go into the audit log too, under the PC's user name."""
    import getpass
    from . import auth as A
    try:
        who = getpass.getuser()
    except Exception:
        who = "unknown"
    A.audit(store, {"username": f"command line ({who})"}, action, target, detail, "")


def _users(store: Store, args) -> int:
    from getpass import getpass
    from . import auth as A
    if args.action == "list":
        rows = [(a["username"], a["display_name"], a["role"], "disabled" if a["disabled"] else "",
                 "yes" if a["has_password"] else "", a["tokens"], (a["last_seen_at"] or "never")[:16])
                for a in A.accounts(store)]
        print(_table(rows, ["user", "name", "role", "", "password", "tokens", "last seen"]) or "no accounts yet")
        return 0
    if not args.username:
        raise SystemExit(f"users {args.action} needs a user name")
    password = None
    if args.password:
        password = getpass("Password (10 or more characters): ")
        if password != getpass("Again: "):
            raise SystemExit("the two passwords differ")
    try:
        if args.action == "add":
            made = A.add_account(store, args.username, args.role or "viewer", args.display_name or "", password)
            _audit(store, "admin.users.add", made["username"], {"role": made["role"]})
            print(f"added {made['username']} as {made['role']}")
        else:
            disabled = True if args.disable else False if args.enable else None
            changed = A.update_account(store, args.username, args.role, args.display_name, password, disabled)
            _audit(store, "admin.users.set", changed["username"],
                   {"role": args.role, "disabled": disabled, "password": "changed" if password else None})
            print(f"{changed['username']}: {changed['role']}" + (", disabled" if changed["disabled"] else ""))
    except (KeyError, ValueError) as err:
        raise SystemExit(str(err).strip("'\"")) from None
    return 0


def _companion(out: str) -> int:
    from importlib import resources
    from pathlib import Path
    target = Path(out)
    target.mkdir(parents=True, exist_ok=True)
    source = resources.files(__package__).joinpath("companion")
    for name in ("PowerBIWorkspaceCompanion.ps1", "install-companion.ps1", "PowerBIWorkspace.pbitool.template.json"):
        data = source.joinpath(name).read_bytes()
        if name.endswith(".ps1"):  # Windows PowerShell 5.1 reads a script without a byte order mark as ANSI
            data = b"\xef\xbb\xbf" + data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        (target / name).write_bytes(data)
    print(f"wrote the companion to {target.resolve()}\n"
          "On each Windows PC with Power BI Desktop, from that folder, in PowerShell as administrator:\n"
          "  powershell -ExecutionPolicy Bypass -File .\\install-companion.ps1 -Workspace http://127.0.0.1:8766 -DownloadTom\n"
          "then restart Power BI Desktop: 'Workspace changes' appears under External Tools.")
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
