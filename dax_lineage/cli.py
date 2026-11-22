"""Command line for the lineage tool.

    daxlin build  exports/              -o lineage.html
    daxlin build  info_measures.csv --tables info_tables.csv --columns info_columns.csv
    daxlin deps   "Sales YoY %"
    daxlin used-by "Total Sales"
    daxlin check
    daxlin json   -o lineage.json
    daxlin dot    -o lineage.dot

Run the explorer on this machine, with nothing sent anywhere:
    daxlin serve                         # http://127.0.0.1:8765
    daxlin offline -o explorer.html      # one file to open from disk, no server

Follow the measure you edit in Power BI Desktop, on a second screen:
    daxlin live                          # http://127.0.0.1:8770

A catalogue of every model the team keeps (see `daxlin workspace --help`):
    daxlin workspace add-folder "C:/Users/me/Contoso/BI - Reports"
    daxlin workspace scan
    daxlin workspace serve                       # http://127.0.0.1:8766

With the report, for where-used and unused measures:
    daxlin unused     -s model.xlsx --report "Sales.pbix"   # exits 1 if any are unused
    daxlin where-used "Total Sales" -s model.xlsx --report "Sales.pbix"
    daxlin unused     -s "Sales.pbit"                        # a .pbit carries both
    daxlin unused     -s "C:/projects/Sales"                 # a PBIP folder with model.bim
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .graph import build_graph, measure_id
from .load import autodiscover, catalogue_from_rows, load_catalogue, load_workbook
from .pbi_report import is_report_path, read_report
from .report import render_dot, render_html, render_json, render_mermaid, render_tree


def _inputs(args) -> dict:
    """Work out which export files to read from a path or a folder."""
    source = Path(args.source)
    if source.is_dir():
        found = autodiscover(source)
        if "measures_path" not in found:
            raise SystemExit(
                f"{source}: no file whose name contains 'measure'. "
                "Name the INFO.MEASURES export info_measures.csv, or pass the file directly."
            )
        for key, flag in (
            ("tables_path", args.tables),
            ("columns_path", args.columns),
            ("relationships_path", args.relationships),
        ):
            if flag:
                found[key] = Path(flag)
        return found
    return {
        "measures_path": source,
        "tables_path": Path(args.tables) if args.tables else None,
        "columns_path": Path(args.columns) if args.columns else None,
        "relationships_path": Path(args.relationships) if args.relationships else None,
    }


def _read_reports(paths) -> tuple:
    usages, model, notes = [], None, []
    for path in paths:
        result = read_report(path)
        if result is None or (not result.usages and not result.model):
            raise SystemExit(f"{path}: no Power BI report or model found in it")
        usages.extend(result.usages)
        model = model or result.model
        if result.note:
            notes.append(result.note)
    return usages, model, notes


def _load(args):
    """Model from INFO.* exports, a .pbit, model.bim or PBIP folder; usage from any reports."""
    source = Path(args.source)
    reports = list(args.report or [])
    is_info_folder = source.is_dir() and "measures_path" in autodiscover(source)
    source_is_report = (source.is_dir() and not is_info_folder) or (not source.is_dir() and is_report_path(source))
    if source_is_report:
        if source.is_dir():
            probe = read_report(source)
            if probe is None or (not probe.usages and not probe.model):
                raise SystemExit(
                    f"{source}: no file whose name contains 'measure' (the INFO.MEASURES export), "
                    "and no Power BI project in it (a .Report folder or model.bim)."
                )
        reports.insert(0, source)
    usages, json_model, notes = _read_reports(reports)

    if source_is_report:
        if not json_model:
            raise SystemExit(
                (notes[0] if notes else f"{source}: no model in it.")
                + " Pass the INFO.MEASURES export with -s and this file with --report."
            )
        catalogue = catalogue_from_rows(json_model)
        names = source.name
    elif source.suffix.lower() in {".xlsx", ".xlsm"}:
        catalogue = load_workbook(source)
        names = source.name
    else:
        paths = _inputs(args)
        catalogue = load_catalogue(**paths)
        names = ", ".join(sorted({Path(p).name for p in paths.values() if p}))
    if args.report:
        names += " + " + ", ".join(Path(r).name for r in args.report)
    # relationships always mark key columns as used; they are drawn only on request
    lineage = build_graph(catalogue, include_relationships=args.with_relationships, usages=usages or None)
    return lineage, names


def _find(lineage, name: str) -> str:
    key = measure_id(name)
    if key in lineage.nodes:
        return key
    wanted = name.strip().casefold()
    for node_id, node in lineage.nodes.items():
        if node.label.casefold() == wanted or node.name.casefold() == wanted:
            return node_id
    partial = [n for n in lineage.nodes.values() if wanted in n.label.casefold()]
    if len(partial) == 1:
        return partial[0].id
    if partial:
        raise SystemExit(
            "that name matches several objects:\n  "
            + "\n  ".join(sorted(n.label for n in partial[:15]))
        )
    raise SystemExit(f"no measure or column called {name!r} in the export")


def _write(text: str, out, default_name: str) -> None:
    if out in (None, "-"):
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return
    path = Path(out)
    path.write_text(text, encoding="utf-8")
    size = path.stat().st_size
    print(f"wrote {path} ({size / 1024:.0f} KB)", file=sys.stderr)


def main(argv=None) -> int:
    # an older Windows console code page (cp437, cp850) cannot show every
    # character a model may use; print what it can instead of crashing
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["live"]:
        import argparse as _argparse
        from .live import DEFAULT_PORT, serve as live_serve
        live_args = _argparse.ArgumentParser(
            prog="daxlin live",
            description="Follow the measure you edit in Power BI Desktop, on a second screen. "
                        "Start 'Live lineage' from Desktop's External Tools ribbon to feed it.")
        live_args.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
        live_args.add_argument("--report", help="saved .pbix/.pbit/PBIP to read visuals from, when the watcher "
                                                "cannot tell which file Desktop has open")
        live_args.add_argument("--no-open", action="store_true", help="do not open a browser")
        options = live_args.parse_args(argv[1:])
        return live_serve(options.port, options.report, open_browser=not options.no_open)
    if argv[:1] == ["workspace"]:
        from .workspace.cli import main as workspace_main
        return workspace_main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="daxlin",
        description="Read DAX lineage out of INFO.MEASURES() query output and draw it.",
    )
    parser.add_argument("command", choices=["build", "json", "dot", "mermaid", "deps", "used-by", "check", "list",
                                            "unused", "where-used", "serve", "offline"])
    parser.add_argument("target", nargs="?", help="measure or column name, for deps / used-by")
    parser.add_argument("-s", "--source", default="examples",
                        help="the INFO.MEASURES export, an .xlsx workbook with a sheet per INFO.* query, "
                             "or a folder of exports (default: examples)")
    parser.add_argument("--tables", help="INFO.TABLES export")
    parser.add_argument("--columns", help="INFO.COLUMNS export")
    parser.add_argument("--relationships", help="INFO.RELATIONSHIPS export")
    parser.add_argument("-r", "--report", action="append", metavar="PATH",
                        help="a Power BI report (.pbix, .pbit, PBIP folder or its zip, report layout .json); repeatable")
    parser.add_argument("--columns-too", action="store_true", help="unused: list unused columns as well")
    parser.add_argument("--with-relationships", action="store_true",
                        help="also draw table-to-table relationship edges")
    parser.add_argument("-o", "--out", help="output file; '-' for stdout")
    parser.add_argument("--title", default="DAX measure lineage", help="title for the HTML page")
    parser.add_argument("--depth", type=int, default=12, help="depth limit for deps / used-by")
    parser.add_argument("--host", default="127.0.0.1", help="serve: address to listen on (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="serve: port (default 8765)")
    parser.add_argument("--no-open", action="store_true", help="serve: do not open a browser")
    parser.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                        help="serve: a host name the page may be reached under (behind a proxy or in Docker)")
    args = parser.parse_args(argv)

    if args.command == "serve":
        from .serve import serve
        return serve(args.host, args.port, open_browser=not args.no_open, allow_hosts=args.allow_host)
    if args.command == "offline":
        from .offline import packaged
        _write(packaged(), args.out or "dax-lineage-explorer.html", "dax-lineage-explorer.html")
        return 0

    lineage, source_names = _load(args)

    if args.command == "build":
        _write(render_html(lineage, args.title, source_names), args.out or "lineage.html", "lineage.html")
    elif args.command == "json":
        _write(render_json(lineage), args.out, "lineage.json")
    elif args.command == "dot":
        _write(render_dot(lineage), args.out, "lineage.dot")
    elif args.command == "mermaid":
        ids = None
        if args.target:
            node_id = _find(lineage, args.target)
            ids = [node_id, *lineage.upstream(node_id), *lineage.downstream(node_id)]
        _write(render_mermaid(lineage, ids), args.out, "lineage.mmd")
    elif args.command == "deps":
        if not args.target:
            raise SystemExit("deps needs a measure name")
        _write(render_tree(lineage, _find(lineage, args.target), "up", args.depth), args.out, "deps.txt")
    elif args.command == "used-by":
        if not args.target:
            raise SystemExit("used-by needs a measure name")
        _write(render_tree(lineage, _find(lineage, args.target), "down", args.depth), args.out, "used-by.txt")
    elif args.command == "list":
        rows = sorted(
            (n for n in lineage.nodes.values() if n.kind == "measure"),
            key=lambda n: (-n.depth, n.name),
        )
        width = max((len(n.name) for n in rows), default=4)
        lines = [f"{'measure'.ljust(width)}  depth  reads  read by  table"]
        for node in rows:
            lines.append(
                f"{node.name.ljust(width)}  {node.depth:>5}  "
                f"{len(lineage.dependencies(node.id)):>5}  {len(lineage.dependents(node.id)):>7}  {node.table}"
            )
        _write("\n".join(lines), args.out, "measures.txt")
    elif args.command == "unused":
        return _unused(lineage, args)
    elif args.command == "where-used":
        if not args.target:
            raise SystemExit("where-used needs a measure or column name")
        _write(_where_used(lineage, _find(lineage, args.target)), args.out, "where-used.txt")
    elif args.command == "check":
        stats = lineage.stats()
        problems = 0
        print(f"{stats['measures']} measures, {stats['columns']} columns, "
              f"{stats['edges']} dependencies, deepest chain {stats['maxDepth']}")
        if stats["cycles"]:
            problems += len(stats["cycles"])
            print(f"\ncircular dependencies ({len(stats['cycles'])}):")
            for cycle in stats["cycles"]:
                print("  " + " -> ".join(lineage.nodes[i].label for i in cycle))
        if stats["unresolvedReferences"]:
            problems += len(stats["unresolvedReferences"])
            print(f"\nunresolved references ({len(stats['unresolvedReferences'])}):")
            for name in stats["unresolvedReferences"]:
                print(f"  [{name}]")
            print("  (export INFO.COLUMNS as well if these are column names)")
        missing = [n for n in lineage.nodes.values() if not n.exists]
        if missing:
            problems += len(missing)
            print(f"\nreferenced but not in the export ({len(missing)}):")
            for node in missing:
                users = ", ".join(lineage.nodes[u].label for u in lineage.dependents(node.id)[:4])
                print(f"  {node.label}  <- {users}")
        report = stats.get("report")
        if report and report["brokenVisualRefs"]:
            problems += len(report["brokenVisualRefs"])
            print(f"\nvisual fields missing from the model ({len(report['brokenVisualRefs'])}):")
            for broken in report["brokenVisualRefs"]:
                print(f"  {broken['visual']}: {broken['field']}")
        errors = [n for n in lineage.nodes.values() if n.state and n.state not in ("Ready", "")]
        if errors:
            problems += len(errors)
            print(f"\nmeasures not in the Ready state ({len(errors)}):")
            for node in errors:
                print(f"  {node.label}: {node.state} {node.error_message}")
        if stats["unusedByOtherMeasures"]:
            print(f"\nleaf measures, not read by any other measure "
                  f"({len(stats['unusedByOtherMeasures'])}): "
                  + ", ".join(stats["unusedByOtherMeasures"][:12]))
        if report:
            m = report["measures"]
            print(f"\nusage in {', '.join(report['reports'])}: {m['visual']} measures in visuals, "
                  f"{m['indirect']} through other measures, {m['unused'] + m['deadChain']} unused "
                  f"(run `daxlin unused` for the list)")
        print("\nno problems found" if not problems else f"\n{problems} thing(s) to look at")
        return 1 if problems else 0
    return 0


STATUS_TEXT = {
    "visual": "used in visuals",
    "indirect": "not in a visual, used through other measures",
    "structural": "nothing reads it, but the model needs it",
    "deadChain": "unused: only unused measures read it",
    "unused": "unused: nothing reads it",
}


def _need_report(lineage) -> None:
    if not lineage.has_report:
        raise SystemExit(
            "this needs the report: add --report with a .pbix, .pbit or PBIP folder, "
            "or pass a .pbit / PBIP folder with -s"
        )


def _unused(lineage, args) -> int:
    _need_report(lineage)
    stats = lineage.stats()["report"]
    rows = [n for n in lineage.unused() if n.kind == "measure" or args.columns_too]
    rows.sort(key=lambda n: (n.kind != "measure", n.usage["status"] != "unused", n.table, n.name))
    lines = [f"Usage in {', '.join(stats['reports'])}: {stats['visuals']} visuals on {stats['pages']} pages"]
    if not rows:
        lines.append("\nevery measure is used" + (" and every column" if args.columns_too else ""))
    else:
        width = max(len(n.label) for n in rows)
        lines.append("")
        lines.append(f"{'name'.ljust(width)}  {'kind':7}  status")
        for n in rows:
            readers = [lineage.nodes[r].name for r in lineage.dependents(n.id) if lineage.nodes[r].kind != "visual"]
            extra = f"  (read by {', '.join(readers)})" if readers else ""
            lines.append(f"{n.label.ljust(width)}  {n.kind:7}  {STATUS_TEXT[n.usage['status']]}{extra}")
        lines.append("\nUnused in the reports given. Other reports on the same semantic model, Excel and")
        lines.append("paginated reports are not seen: check them before deleting.")
    _write("\n".join(lines), args.out, "unused.txt")
    return 1 if any(n.kind == "measure" for n in rows) else 0


def _where_used(lineage, node_id: str) -> str:
    _need_report(lineage)
    node = lineage.nodes[node_id]
    if node.usage is None:
        raise SystemExit(f"{node.label} is a {node.kind}; where-used is for measures and columns")
    u = node.usage
    roles = {(e.source, e.target): e.roles for e in lineage.edges if e.kind == "used_in_visual"}
    lines = [f"{node.label}  ({node.kind}{' on ' + node.table if node.table and node.kind == 'measure' else ''})",
             f"status: {STATUS_TEXT[u['status']]}"]
    if u["structural"]:
        lines.append("model needs it: " + "; ".join(u["structural"]))
    if u["visuals"]:
        lines.append(f"\nin visuals ({len(u['visuals'])}):")
        by_page: dict = {}
        for vid in u["visuals"]:
            by_page.setdefault(lineage.nodes[vid].page, []).append(vid)
        for page, vids in sorted(by_page.items(), key=lambda kv: lineage.nodes[kv[1][0]].page_order):
            lines.append(f"  {page}")
            for vid in vids:
                v = lineage.nodes[vid]
                lines.append(f"    {v.title or v.name}  [{v.visual_type}]  {', '.join(roles.get((vid, node_id), []))}")
    if u["via"]:
        lines.append(f"\nthrough other measures ({len(u['via'])}):")
        for item in u["via"]:
            v, through = lineage.nodes[item["visual"]], lineage.nodes[item["through"]]
            lines.append(f"  {v.page} > {v.title or v.name}  [{v.visual_type}]  via {through.name}")
    readers = [lineage.nodes[r].label for r in lineage.dependents(node_id) if lineage.nodes[r].kind != "visual"]
    if readers:
        lines.append(f"\nread by measures and columns ({len(readers)}): " + ", ".join(readers))
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
