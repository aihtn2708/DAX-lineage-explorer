"""Build the downloadable sample workbooks from the CSV exports in examples/.

    python3 scripts/make_samples.py

Each workbook has one sheet per INFO.* query, named after the function, which is
the shape you get by pasting each DAX query view result into its own sheet.
"""

from __future__ import annotations

import csv
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "samples"

SHEETS = [
    ("INFO.MEASURES", "info_measures.csv"),
    ("INFO.TABLES", "info_tables.csv"),
    ("INFO.COLUMNS", "info_columns.csv"),
    ("INFO.RELATIONSHIPS", "info_relationships.csv"),
]
BOOL_COLUMNS = {"IsHidden", "IsActive", "IsSimpleMeasure"}
INT_COLUMNS = {"ID", "TableID", "FromTableID", "FromColumnID", "ToTableID", "ToColumnID",
               "FromCardinality", "ToCardinality", "SortByColumnID"}


def coerce(column: str, value: str):
    if column in BOOL_COLUMNS:
        return value.strip().casefold() in {"true", "1", "yes"}
    if column in INT_COLUMNS and value.strip().lstrip("-").isdigit():
        return int(value)
    return value


def build(source: Path, target: Path) -> None:
    book = Workbook()
    book.remove(book.active)
    header_fill = PatternFill("solid", fgColor="E6F0FC")
    for sheet_name, file_name in SHEETS:
        path = source / file_name
        if not path.exists():
            continue
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        if not rows:
            continue
        sheet = book.create_sheet(sheet_name)
        header, body = rows[0], rows[1:]
        sheet.append(header)
        for row in body:
            sheet.append([coerce(c, v) for c, v in zip(header, row)])
        for cell in sheet[1]:
            cell.font = Font(bold=True)
            cell.fill = header_fill
        sheet.freeze_panes = "A2"
        for index, name in enumerate(header, start=1):
            longest = max([len(name)] + [len(str(r[index - 1]).split("\n")[0]) for r in body if len(r) >= index])
            sheet.column_dimensions[get_column_letter(index)].width = min(60, max(8, longest + 2))
            if name == "Expression":
                for cell in sheet[get_column_letter(index)][1:]:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
    target.parent.mkdir(parents=True, exist_ok=True)
    book.save(target)
    print(f"wrote {target.relative_to(ROOT)}  ({target.stat().st_size // 1024} KB, sheets: {book.sheetnames})")


def copy_queries() -> None:
    """The site offers the .dax files for download; keep them in step with queries/."""
    target = ROOT / "docs" / "queries"
    target.mkdir(parents=True, exist_ok=True)
    for query in sorted((ROOT / "queries").glob("*.dax")):
        (target / query.name).write_text(query.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"copied queries/{query.name} -> docs/queries/")


if __name__ == "__main__":
    build(ROOT / "examples", OUT / "info_sample.xlsx")
    build(ROOT / "examples" / "troubled", OUT / "info_sample_with_problems.xlsx")
    copy_queries()
