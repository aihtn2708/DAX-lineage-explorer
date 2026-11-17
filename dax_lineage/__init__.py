"""Read DAX lineage from the output of the INFO.* DAX functions and draw it.

    from dax_lineage import load_catalogue, build_graph, render_html

    catalogue = load_catalogue("info_measures.csv", columns_path="info_columns.csv")
    lineage = build_graph(catalogue)
    Path("lineage.html").write_text(render_html(lineage))
"""

from .dax import Reference, scan, tokenize
from .graph import Lineage, Node, build_graph, column_id, measure_id, table_id
from .load import (autodiscover, detect_kind, load_catalogue, load_columns, load_measures,
                   load_tables, load_workbook, read_rows, read_xlsx, rows_from_matrix)
from .model import Catalogue, Column, Measure, Relationship, Table
from .report import render_dot, render_html, render_json, render_mermaid, render_tree

__version__ = "0.1.0"
__all__ = [
    "Catalogue", "Column", "Lineage", "Measure", "Node", "Reference", "Relationship", "Table",
    "autodiscover", "build_graph", "detect_kind", "load_workbook", "read_xlsx", "rows_from_matrix", "column_id", "load_catalogue", "load_columns", "load_measures",
    "load_tables", "measure_id", "read_rows", "render_dot", "render_html", "render_json",
    "render_mermaid", "render_tree", "scan", "table_id", "tokenize",
]
