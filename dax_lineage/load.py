"""Load the result of the INFO.* queries, however the grid was exported.

Accepted shapes
---------------
CSV / TSV   header row, one row per object. Delimiter is sniffed.
JSON        a bare list of row objects, or the Power BI REST
            ``executeQueries`` envelope ``{"results":[{"tables":[{"rows":[...]}]}]}``,
            or ``{"tables":[{"rows":[...]}]}`` as written by some clients.

Header names are matched loosely, so all of these mean the same column:
``Name``, ``[Name]``, ``Measures[Name]``, ``INFO.MEASURES()[Name]``, ``name``.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Iterable, Optional

from .model import Catalogue, Column, Measure, Relationship, Table

_BRACKET = re.compile(r"\[([^\]]*)\]\s*$")


def _header_key(raw: str) -> str:
    """'Measures[Display Folder]' -> 'displayfolder'."""
    text = (raw or "").strip().lstrip("﻿")
    hit = _BRACKET.search(text)
    if hit:
        text = hit.group(1)
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _rows_from_json(payload) -> list:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        if "results" in payload:
            rows = []
            for result in payload.get("results") or []:
                for table in result.get("tables") or []:
                    rows.extend(table.get("rows") or [])
            return rows
        for key in ("tables", "rows", "data", "value"):
            if key in payload:
                inner = payload[key]
                if key == "tables":
                    rows = []
                    for table in inner or []:
                        rows.extend(table.get("rows") or [])
                    return rows
                if isinstance(inner, list):
                    return [r for r in inner if isinstance(r, dict)]
    raise ValueError("could not find a list of rows in that JSON file")


def _rows_from_delimited(text: str) -> list:
    text = text.lstrip("﻿")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    return list(csv.DictReader(io.StringIO(text), delimiter=delimiter))


def read_rows(path) -> list:
    """Return the export at ``path`` as a list of dicts keyed by loose header name."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    stripped = text.lstrip()
    if path.suffix.lower() == ".json" or stripped[:1] in "[{":
        raw_rows = _rows_from_json(json.loads(text))
    else:
        raw_rows = _rows_from_delimited(text)
    out = []
    for row in raw_rows:
        out.append({_header_key(k): v for k, v in row.items() if k is not None})
    return out


# -- field coercion ---------------------------------------------------------

_TRUE = {"true", "1", "yes", "y", "t"}
_FALSE = {"false", "0", "no", "n", "f", ""}


def _pick(row: dict, *names: str):
    for name in names:
        key = _header_key(name)
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _text(row: dict, *names: str) -> str:
    value = _pick(row, *names)
    if value is None:
        return ""
    return str(value).strip()


def _int(row: dict, *names: str) -> Optional[int]:
    value = _pick(row, *names)
    if value is None:
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _bool(row: dict, *names: str) -> bool:
    value = _pick(row, *names)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return bool(text)


# -- per-query loaders ------------------------------------------------------


def load_measures(path) -> list:
    rows = read_rows(path)
    measures = []
    for row in rows:
        name = _text(row, "Name", "MeasureName")
        if not name:
            continue
        measures.append(
            Measure(
                id=_int(row, "ID"),
                table_id=_int(row, "TableID"),
                name=name,
                expression=_text(row, "Expression", "MeasureExpression"),
                table_name=_text(row, "Table", "TableName"),
                description=_text(row, "Description"),
                format_string=_text(row, "FormatString"),
                display_folder=_text(row, "DisplayFolder"),
                is_hidden=_bool(row, "IsHidden"),
                state=_text(row, "State"),
                error_message=_text(row, "ErrorMessage"),
                data_type=_text(row, "DataType"),
                modified_time=_text(row, "ModifiedTime"),
                lineage_tag=_text(row, "LineageTag"),
            )
        )
    if not measures:
        raise ValueError(
            f"{Path(path).name}: no rows with a [Name] column. "
            "Export the result of EVALUATE INFO.MEASURES() with its header row."
        )
    return measures


def load_tables(path) -> list:
    out = []
    for row in read_rows(path):
        name = _text(row, "Name", "TableName")
        if not name:
            continue
        out.append(
            Table(
                id=_int(row, "ID"),
                name=name,
                description=_text(row, "Description"),
                is_hidden=_bool(row, "IsHidden"),
                lineage_tag=_text(row, "LineageTag"),
            )
        )
    return out


def load_columns(path) -> list:
    out = []
    for row in read_rows(path):
        name = _text(row, "ExplicitName", "InferredName", "Name", "ColumnName")
        if not name:
            continue
        out.append(
            Column(
                id=_int(row, "ID"),
                table_id=_int(row, "TableID"),
                name=name,
                table_name=_text(row, "Table", "TableName"),
                expression=_text(row, "Expression"),
                data_type=_text(row, "DataType", "ExplicitDataType", "InferredDataType"),
                column_type=_text(row, "Type", "ColumnType"),
                is_hidden=_bool(row, "IsHidden"),
                display_folder=_text(row, "DisplayFolder"),
                lineage_tag=_text(row, "LineageTag"),
            )
        )
    return out


def load_relationships(path) -> list:
    out = []
    for row in read_rows(path):
        out.append(
            Relationship(
                id=_int(row, "ID"),
                from_table_id=_int(row, "FromTableID"),
                from_column_id=_int(row, "FromColumnID"),
                to_table_id=_int(row, "ToTableID"),
                to_column_id=_int(row, "ToColumnID"),
                is_active=_bool(row, "IsActive"),
                cross_filtering=_text(row, "CrossFilteringBehavior"),
            )
        )
    return out


def load_catalogue(
    measures_path,
    tables_path=None,
    columns_path=None,
    relationships_path=None,
) -> Catalogue:
    """Load every export that was supplied and cross-resolve the ids."""
    catalogue = Catalogue(
        measures=load_measures(measures_path),
        tables=load_tables(tables_path) if tables_path else [],
        columns=load_columns(columns_path) if columns_path else [],
        relationships=load_relationships(relationships_path) if relationships_path else [],
    )
    return catalogue.resolve()


def autodiscover(directory) -> dict:
    """Find INFO.* exports in a folder by file name, so one path is enough."""
    directory = Path(directory)
    wanted = {
        "measures_path": ("measure",),
        "tables_path": ("table",),
        "columns_path": ("column",),
        "relationships_path": ("relationship", "relation"),
    }
    found: dict = {}
    candidates = [
        p for p in sorted(directory.iterdir())
        if p.is_file() and p.suffix.lower() in {".csv", ".tsv", ".txt", ".json"}
    ]
    for arg, needles in wanted.items():
        for path in candidates:
            stem = path.stem.casefold()
            if any(n in stem for n in needles):
                found[arg] = path
                break
    return found


# -- Excel workbooks --------------------------------------------------------
# Read with the standard library only (zipfile + ElementTree): an .xlsx is a zip
# of XML parts. Values only; formulas come back as their cached result.

_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _column_index(ref: str) -> int:
    letters = re.match(r"[A-Za-z]*", ref or "").group(0).upper()
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def read_xlsx(path) -> list:
    """Return ``[(sheet_name, matrix)]`` for every sheet in an .xlsx workbook."""
    import zipfile
    import xml.etree.ElementTree as ET

    path = Path(path)
    with open(path, "rb") as handle:
        if handle.read(4) == b"\xd0\xcf\x11\xe0":
            raise ValueError(f"{path.name} is an old .xls workbook; save it as .xlsx or CSV")
    with zipfile.ZipFile(path) as book:
        names = set(book.namelist())

        def xml(part):
            part = part.lstrip("/")
            return ET.fromstring(book.read(part)) if part in names else None

        def text_of(node):
            return "".join(t.text or "" for t in node.iter(f"{_NS_MAIN}t"))

        shared = []
        sst = xml("xl/sharedStrings.xml")
        if sst is not None:
            shared = [text_of(si) for si in sst.findall(f"{_NS_MAIN}si")]
        rels = {}
        rel_root = xml("xl/_rels/workbook.xml.rels")
        if rel_root is not None:
            rels = {r.get("Id"): r.get("Target") for r in rel_root.findall(f"{_NS_PKG}Relationship")}

        out = []
        workbook = xml("xl/workbook.xml")
        if workbook is None:
            raise ValueError(f"{path.name}: no workbook inside; is it an .xlsx?")
        for number, sheet in enumerate(workbook.iter(f"{_NS_MAIN}sheet"), start=1):
            target = rels.get(sheet.get(f"{_NS_REL}id"), f"worksheets/sheet{number}.xml")
            part = target.lstrip("/") if target.startswith("/") else "xl/" + target
            part = "/".join(p for p in part.split("/") if p not in ("", "."))
            root = xml(part)
            if root is None:
                continue
            matrix, cursor = [], 0
            for row in root.iter(f"{_NS_MAIN}row"):
                r = row.get("r")
                cursor = int(r) - 1 if r and r.isdigit() else cursor
                line, col = [], 0
                for cell in row.findall(f"{_NS_MAIN}c"):
                    col = _column_index(cell.get("r")) if cell.get("r") else col
                    kind = cell.get("t", "n")
                    v = cell.find(f"{_NS_MAIN}v")
                    raw = v.text if v is not None and v.text is not None else ""
                    if kind == "s":
                        value = shared[int(raw)] if raw.isdigit() and int(raw) < len(shared) else ""
                    elif kind == "inlineStr":
                        inline = cell.find(f"{_NS_MAIN}is")
                        value = text_of(inline if inline is not None else cell)
                    elif kind == "b":
                        value = raw == "1"
                    elif kind in ("str", "e"):
                        value = raw
                    else:
                        value = raw if raw == "" else (int(float(raw)) if float(raw).is_integer() else float(raw))
                    while len(line) < col:
                        line.append("")
                    line.append(value)
                    col += 1
                while len(matrix) < cursor:
                    matrix.append([])
                matrix.append(line)
                cursor += 1
            out.append((sheet.get("name") or f"Sheet{number}", matrix))
        return out


_KNOWN_HEADERS = {"name", "explicitname", "inferredname", "expression", "id", "fromtableid"}


def rows_from_matrix(matrix) -> list:
    """Grid to row dicts, finding the header row even below a title line."""
    header_at = 0
    for index, line in enumerate(matrix[:10]):
        keys = [_header_key(str(c)) for c in line]
        if any(k in _KNOWN_HEADERS for k in keys) and len([k for k in keys if k]) >= 2:
            header_at = index
            break
    if not matrix:
        return []
    header = [str(c) if c is not None else "" for c in matrix[header_at]]
    rows = []
    for line in matrix[header_at + 1:]:
        if not any(str(c).strip() for c in line if c is not None):
            continue
        rows.append({h: (line[i] if i < len(line) else "") for i, h in enumerate(header) if h})
    return rows


def detect_kind(rows, label: str = ""):
    """Which INFO.* query a sheet holds: the same rules as the web page."""
    keys = {_header_key(k) for k in (rows[0] if rows else {})}
    name = (label or "").casefold()
    if keys & {"fromtableid", "fromcolumnid"}:
        return "relationships"
    if keys & {"explicitname", "inferredname"}:
        return "columns"
    if keys & {"issimplemeasure", "kpiid", "detailrowsdefinitionid"}:
        return "measures"
    if keys & {"modelid", "tablestorageid", "calculationgroupid"}:
        return "tables"
    for word, kind in (("relationship", "relationships"), ("column", "columns"),
                       ("measure", "measures"), ("table", "tables")):
        if word in name:
            return kind
    if {"name", "expression"} <= keys:
        return "measures"
    if "name" in keys:
        return "tables"
    return None


def load_workbook(path) -> Catalogue:
    """Load every INFO.* sheet of one .xlsx workbook."""
    import tempfile

    sheets = [(n, m) for n, m in read_xlsx(path) if any(any(str(c).strip() for c in l) for l in m)]
    found: dict = {}
    for sheet_name, matrix in sheets:
        rows = rows_from_matrix(matrix)
        label = f"{sheet_name} {Path(path).name}" if len(sheets) == 1 else sheet_name
        kind = detect_kind(rows, label)
        if kind and rows and kind not in found:
            found[kind] = rows
    if "measures" not in found:
        raise ValueError(f"{Path(path).name}: no sheet with INFO.MEASURES rows (Name and Expression columns)")
    # reuse the per-query loaders through a JSON round trip
    paths = {}
    with tempfile.TemporaryDirectory() as tmp:
        for kind, rows in found.items():
            target = Path(tmp) / f"{kind}.json"
            target.write_text(json.dumps(rows, default=str), encoding="utf-8")
            paths[f"{kind}_path"] = target
        return load_catalogue(**paths)
