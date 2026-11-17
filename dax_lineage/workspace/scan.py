"""Scan folders of Power BI files into the workspace database.

A file is read only when it changed: same size and modified time as the
current version means unchanged (so OneDrive does not download files that
are only online), and otherwise the SHA-256 decides. Each change becomes a
new version holding the model's objects, dependencies, visuals and usage.

Files are only ever opened for reading.
"""

from __future__ import annotations

import hashlib
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from ..graph import build_graph, column_id, measure_id, table_id
from ..load import catalogue_from_rows
from ..model import Catalogue
from ..pbi_report import read_report
from .store import Store

SUFFIXES = (".pbix", ".pbit")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "$RECYCLE.BIN"}


def find_files(root: Path) -> list:
    """Power BI files under ``root``, skipping Office lock files and hidden folders."""
    root = Path(root)
    if root.is_file():
        return [root] if root.suffix.lower() in SUFFIXES else []
    found = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            if name.startswith(("~$", ".")) or not name.lower().endswith(SUFFIXES):
                continue
            found.append(Path(folder) / name)
    return found


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _modified(stat) -> str:
    return datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")


def _has_data_model(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return any(n.replace("\\", "/") == "DataModel" for n in archive.namelist())
    except zipfile.BadZipFile:
        return False


def analyse(path: Path) -> dict:
    """Read one file into the rows ``Store.record_version`` takes."""
    path = Path(path)
    try:
        result = read_report(path)
    except zipfile.BadZipFile:
        raise ValueError("not a Power BI file (not a zip archive)") from None
    if result is None or (not result.model and not result.usages):
        raise ValueError((result.note if result else "") or "no Power BI model or report found in it")

    if result.model:
        kind, status = "model", "ok"
        catalogue = catalogue_from_rows(result.model)
    else:
        # a thin report on a published model, or a model this reader cannot open yet
        kind = "model" if path.suffix.lower() == ".pbix" and _has_data_model(path) else "report"
        status = "partial" if kind == "model" else "ok"
        catalogue = Catalogue()
    lineage = build_graph(catalogue, usages=result.usages or None)

    tags = {}
    for m in catalogue.measures:
        tags[measure_id(m.name)] = m.lineage_tag
    for c in catalogue.columns:
        tags[column_id(c.table_name, c.name)] = c.lineage_tag
    for t in catalogue.tables:
        tags[table_id(t.name)] = t.lineage_tag
    seen_tags: set = set()
    reads, read_by = {}, {}
    for e in lineage.edges:
        reads[e.source] = reads.get(e.source, 0) + 1
        if e.kind != "used_in_visual":
            read_by[e.target] = read_by.get(e.target, 0) + 1

    objects, visuals = [], []
    for node in lineage.nodes.values():
        if node.kind == "visual":
            visuals.append({"node": node.id, "report": node.report, "page": node.page, "page_order": node.page_order,
                            "visual_type": node.visual_type, "title": node.title})
            continue
        tag = tags.get(node.id, "") or ""
        key = tag if tag and tag not in seen_tags else node.id
        seen_tags.add(tag)
        objects.append({
            "node": node.id, "object_key": key, "kind": node.kind, "table_name": node.table, "name": node.name,
            "expression": node.expression, "description": node.description, "display_folder": node.display_folder,
            "format_string": node.format_string, "data_type": node.data_type, "is_hidden": node.is_hidden,
            "is_calculated": node.is_calculated, "state": node.state, "error_message": node.error_message,
            "depth": node.depth, "in_model": node.exists, "lineage_tag": tag,
            "usage_status": (node.usage or {}).get("status", ""), "usage": node.usage,
            "reads": reads.get(node.id, 0), "read_by": read_by.get(node.id, 0),
        })
    dependencies = [{"source": e.source, "target": e.target, "kind": e.kind, "via_variable": e.via_variable}
                    for e in lineage.edges if e.kind != "used_in_visual"]
    usages = [{"visual": e.source, "field": e.target, "roles": e.roles}
              for e in lineage.edges if e.kind == "used_in_visual"]

    stats = lineage.stats()
    report = stats.get("report") or {}
    counts = {
        "measures": len([o for o in objects if o["kind"] == "measure" and o["in_model"]]),
        "columns": len([o for o in objects if o["kind"] == "column" and o["in_model"]]),
        "tables": len(catalogue.tables),
        "visuals": report.get("visuals", 0), "pages": report.get("pages", 0), "has_report": bool(result.usages),
    }
    if kind == "model" and status == "ok" and result.usages:
        counts["unused_measures"] = len(report.get("unusedMeasures", []))
        counts["unused_columns"] = len([o for o in objects if o["kind"] == "column" and o["in_model"]
                                        and o["usage_status"] in ("unused", "deadChain")])
        counts["broken_refs"] = len(report.get("brokenVisualRefs", []))
    note = result.note
    if kind == "report" and not note:
        note = "A report on a published model: only its visuals are recorded. Fields are matched to models by name."
    return {"kind": kind, "status": status, "note": note, "found": "; ".join(result.found), "counts": counts,
            "objects": objects, "dependencies": dependencies, "visuals": visuals, "usages": usages}


def scan_folders(store: Store, folders: Optional[Iterable] = None, force: bool = False,
                 log: Callable[[str], None] = lambda line: None) -> dict:
    """Scan every registered folder (or the ones given). Returns counts and a log."""
    registered = {f["path"]: f["id"] for f in store.folders()}
    roots = [str(Path(f).expanduser().resolve()) for f in folders] if folders else list(registered)
    summary = {"folders": len(roots), "files": 0, "new_versions": 0, "unchanged": 0, "failed": 0, "missing": 0,
               "log": []}

    def say(line: str):
        summary["log"].append(line)
        log(line)

    if not roots:
        say("no folders to scan: add one with `daxlin workspace add-folder PATH`")
        return summary
    run_id = store.start_run(len(roots))
    try:
        for root in roots:
            root_path = Path(root)
            if not root_path.exists():
                say(f"folder not found, skipped: {root}")
                summary["failed"] += 1
                continue
            seen = []
            for path in find_files(root_path):
                summary["files"] += 1
                resolved = str(path.resolve())
                seen.append(resolved)
                try:
                    _scan_file(store, path, resolved, registered.get(root), force, summary, say)
                except Exception as err:  # one bad file must not stop the scan
                    summary["failed"] += 1
                    say(f"failed  {path.name}: {err}")
            if root_path.is_dir():
                summary["missing"] += store.mark_missing(str(root_path), seen)
        say(f"{summary['files']} files: {summary['new_versions']} new versions, {summary['unchanged']} unchanged, "
            f"{summary['failed']} failed" + (f", {summary['missing']} no longer found" if summary["missing"] else ""))
    finally:
        store.finish_run(run_id, summary)
    return summary


def _scan_file(store: Store, path: Path, resolved: str, folder_id, force: bool, summary: dict, say) -> None:
    stat = path.stat()
    modified = _modified(stat)
    current = store.model_by_path(resolved)
    if (current and not force and current.get("file_size") == stat.st_size
            and current.get("file_modified_at") == modified):
        store.touch(current["id"])
        summary["unchanged"] += 1
        return
    digest = file_hash(path)
    if current and not force and current.get("file_hash") == digest:
        store.touch(current["id"])
        summary["unchanged"] += 1
        return
    try:
        rows = analyse(path)
    except (ValueError, OSError) as err:
        rows = {"kind": (current or {}).get("kind", "model"), "status": "error", "note": str(err), "found": "",
                "counts": {}, "objects": [], "dependencies": [], "visuals": [], "usages": []}
    saved = store.record_version(path=resolved, name=path.stem, folder_id=folder_id, file_hash=digest,
                                 file_size=stat.st_size, file_modified_at=modified, **rows)
    summary["new_versions"] += 1
    if rows["status"] != "error":
        from .check import check_version
        checked = check_version(store, saved["version_id"])
        if checked:
            rows["counts"]["score"] = checked["score"]
    if rows["status"] == "error":
        summary["failed"] += 1
        say(f"error   {path.name} v{saved['number']}: {rows['note']}")
    else:
        c = rows["counts"]
        what = (f"{c.get('measures', 0)} measures, {c.get('visuals', 0)} visuals"
                + (f", {c['unused_measures']} unused measures" if c.get("unused_measures") else "")
                + (f", standards score {c['score']}" if "score" in c else ""))
        say(f"{'new' if not current else 'changed'}{' ' * (8 - len('new' if not current else 'changed'))}"
            f"{path.name} v{saved['number']}: {what}" + (f" ({rows['note']})" if rows["status"] != "ok" else ""))
