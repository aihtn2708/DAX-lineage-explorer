"""Command line for the lineage tool.

    daxlin build  exports/              -o lineage.html
    daxlin build  info_measures.csv --tables info_tables.csv --columns info_columns.csv
    daxlin deps   "Sales YoY %"
    daxlin used-by "Total Sales"
    daxlin check
    daxlin json   -o lineage.json
    daxlin dot    -o lineage.dot
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .graph import build_graph, measure_id
from .load import autodiscover, load_catalogue, load_workbook
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
        if not args.with_relationships:
            found.pop("relationships_path", None)
        return found
    return {
        "measures_path": source,
        "tables_path": Path(args.tables) if args.tables else None,
        "columns_path": Path(args.columns) if args.columns else None,
        "relationships_path": (
            Path(args.relationships)
            if args.relationships and args.with_relationships
            else None
        ),
    }


def _load(args):
    source = Path(args.source)
    if source.suffix.lower() in {".xlsx", ".xlsm"}:
        catalogue = load_workbook(source)
        if not args.with_relationships:
            catalogue.relationships = []
        return build_graph(catalogue, include_relationships=args.with_relationships), source.name
    paths = _inputs(args)
    catalogue = load_catalogue(**paths)
    lineage = build_graph(catalogue, include_relationships=bool(paths.get("relationships_path")))
    names = ", ".join(sorted({Path(p).name for p in paths.values() if p}))
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
    parser = argparse.ArgumentParser(
        prog="daxlin",
        description="Read DAX lineage out of INFO.MEASURES() query output and draw it.",
    )
    parser.add_argument("command", choices=["build", "json", "dot", "mermaid", "deps", "used-by", "check", "list"])
    parser.add_argument("target", nargs="?", help="measure or column name, for deps / used-by")
    parser.add_argument("-s", "--source", default="examples",
                        help="the INFO.MEASURES export, an .xlsx workbook with a sheet per INFO.* query, "
                             "or a folder of exports (default: examples)")
    parser.add_argument("--tables", help="INFO.TABLES export")
    parser.add_argument("--columns", help="INFO.COLUMNS export")
    parser.add_argument("--relationships", help="INFO.RELATIONSHIPS export")
    parser.add_argument("--with-relationships", action="store_true",
                        help="also draw table-to-table relationship edges")
    parser.add_argument("-o", "--out", help="output file; '-' for stdout")
    parser.add_argument("--title", default="DAX measure lineage", help="title for the HTML page")
    parser.add_argument("--depth", type=int, default=12, help="depth limit for deps / used-by")
    args = parser.parse_args(argv)

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
        print("\nno problems found" if not problems else f"\n{problems} thing(s) to look at")
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
