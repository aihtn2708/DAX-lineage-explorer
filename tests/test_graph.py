from pathlib import Path

from dax_lineage.graph import build_graph, column_id, measure_id
from dax_lineage.load import load_catalogue
from dax_lineage.model import Catalogue, Column, Measure, Table
from dax_lineage.report import render_dot, render_html, render_json, render_mermaid, render_tree

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def sample():
    return build_graph(load_catalogue(
        EXAMPLES / "info_measures.csv",
        EXAMPLES / "info_tables.csv",
        EXAMPLES / "info_columns.csv",
    ))


def tiny(measures, columns=(), tables=()):
    catalogue = Catalogue(
        measures=[Measure(id=i, table_id=1, name=n, expression=e)
                  for i, (n, e) in enumerate(measures)],
        tables=[Table(id=1, name=t) for t in (tables or ["T"])],
        columns=[Column(id=100 + i, table_id=1, name=c, table_name="T")
                 for i, c in enumerate(columns)],
    ).resolve()
    return build_graph(catalogue)


def test_measure_to_measure_edges():
    lineage = tiny([("A", "SUM ( T[x] )"), ("B", "[A] * 2"), ("C", "[B] + [A]")], columns=["x"])
    assert set(lineage.dependencies(measure_id("C"))) == {measure_id("B"), measure_id("A")}
    assert lineage.dependents(measure_id("A")) == [measure_id("B"), measure_id("C")]


def test_depth_is_the_longest_chain():
    lineage = tiny([("A", "SUM ( T[x] )"), ("B", "[A]"), ("C", "[B]")], columns=["x"])
    assert lineage.nodes[measure_id("A")].depth == 1  # reads a column directly
    assert lineage.nodes[measure_id("C")].depth == 3


def test_cycles_are_reported_not_fatal():
    lineage = tiny([("A", "[B] + 1"), ("B", "[A] + 1")])
    assert lineage.cycles, "a self-referencing pair should be reported"
    assert lineage.stats()["cycles"]


def test_reference_to_a_column_outside_the_export():
    lineage = tiny([("A", "SUM ( T[not_exported] )")], columns=["x"])
    node = lineage.nodes[column_id("T", "not_exported")]
    assert node.exists is False
    assert lineage.stats()["missingObjects"] == 1


def test_unknown_bare_reference_becomes_a_dangling_measure():
    # columns were exported too, so the name belongs to neither catalogue
    lineage = tiny([("A", "[Missing Measure] + 1")], columns=["x"])
    node = lineage.nodes[measure_id("Missing Measure")]
    assert node.exists is False
    assert lineage.stats()["unresolvedReferences"] == ["Missing Measure"]


def test_unknown_bare_reference_without_a_column_catalogue_only_warns():
    # with no INFO.COLUMNS export the name may simply be a column
    lineage = tiny([("A", "[Maybe A Column] + 1")])
    assert measure_id("Maybe A Column") not in lineage.nodes
    assert lineage.stats()["unresolvedReferences"] == ["Maybe A Column"]


def test_calculated_column_lineage_is_connected():
    lineage = sample()
    line_amount = column_id("Sales", "LineAmount")
    assert column_id("Sales", "Quantity") in lineage.dependencies(line_amount)
    # Total Sales reaches the base columns through the calculated column
    assert column_id("Sales", "UnitPrice") in lineage.upstream(measure_id("Total Sales"))


def test_sample_statistics():
    stats = sample().stats()
    assert stats["measures"] == 24
    assert stats["unresolvedReferences"] == []
    assert stats["cycles"] == []
    assert stats["maxDepth"] == 5
    assert stats["baseMeasures"] == [
        "Total Sales", "Total Quantity", "Total Cost", "Order Count", "Customer Count"
    ]
    assert stats["mostReferenced"][0] == {"name": "Total Sales", "count": 15}


def test_hidden_helper_measure_keeps_its_flag():
    lineage = sample()
    assert lineage.nodes[measure_id("_Base Sales Helper")].is_hidden is True


def test_time_intelligence_is_flagged():
    lineage = sample()
    assert lineage.nodes[measure_id("Sales PY")].time_intelligence == ["SAMEPERIODLASTYEAR"]
    assert lineage.nodes[measure_id("Avg Category Margin %")].iterators == ["AVERAGEX"]


def test_renderers_produce_output():
    lineage = sample()
    html = render_html(lineage, "Test", "info_measures.csv")
    assert html.startswith("<!DOCTYPE html>")
    assert "__LINEAGE_DATA__" not in html and "__TITLE__" not in html
    assert "</script>" not in html.split('type="application/json">')[1].split("</script>")[0]
    assert '"measures":24' in html.replace(" ", "")

    assert '"nodes"' in render_json(lineage)
    assert "digraph lineage" in render_dot(lineage)

    mermaid = render_mermaid(lineage, [measure_id("Gross Margin"), measure_id("Total Sales")])
    assert "graph LR" in mermaid and "-->" in mermaid

    tree = render_tree(lineage, measure_id("Gross Margin %"))
    assert tree.splitlines()[0] == "Gross Margin %"
    assert any("Total Cost" in line for line in tree.splitlines())


def test_tree_marks_a_cycle_instead_of_looping():
    lineage = tiny([("A", "[B] + 1"), ("B", "[A] + 1")])
    assert "[cycle]" in render_tree(lineage, measure_id("A"))


def test_columns_are_not_called_missing_without_a_column_catalogue():
    # only INFO.MEASURES was exported: nothing says Sales[Amount] is absent
    lineage = tiny([("A", "SUM ( T[Amount] )")])
    assert lineage.nodes[column_id("T", "Amount")].exists is True
    assert lineage.stats()["missingObjects"] == 0
