from pathlib import Path

import pytest

from dax_lineage.cli import main

EXAMPLES = str(Path(__file__).resolve().parent.parent / "examples")


def test_build_writes_a_page(tmp_path, capsys):
    out = tmp_path / "lineage.html"
    assert main(["build", "-s", EXAMPLES, "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "Total Sales" in text and len(text) > 20_000


def test_check_passes_on_the_sample(capsys):
    assert main(["check", "-s", EXAMPLES]) == 0
    assert "no problems found" in capsys.readouterr().out


def test_deps_prints_a_tree(capsys):
    main(["deps", "Gross Margin", "-s", EXAMPLES, "-o", "-"])
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "Gross Margin"
    assert "Total Cost" in out


def test_list_is_sorted_by_depth(capsys):
    main(["list", "-s", EXAMPLES, "-o", "-"])
    rows = capsys.readouterr().out.splitlines()
    assert rows[0].startswith("measure")
    assert "Sales YoY %" in rows[1] or "Avg Category Margin %" in rows[1]


def test_an_exact_name_wins_over_a_partial_match(capsys):
    # "Sales" is a table and also part of many measure names
    main(["deps", "Sales", "-s", EXAMPLES, "-o", "-"])
    assert capsys.readouterr().out.splitlines()[0] == "Sales (table)"


def test_ambiguous_name_is_explained():
    with pytest.raises(SystemExit, match="several objects"):
        main(["deps", "margin", "-s", EXAMPLES])


def test_unknown_name_is_explained():
    with pytest.raises(SystemExit, match="no measure or column"):
        main(["deps", "Nope", "-s", EXAMPLES])


def test_folder_without_a_measures_export(tmp_path):
    (tmp_path / "other.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="no file whose name contains"):
        main(["check", "-s", str(tmp_path)])


def test_mermaid_for_one_measure(capsys):
    main(["mermaid", "Sales YoY %", "-s", EXAMPLES, "-o", "-"])
    out = capsys.readouterr().out
    assert out.startswith("graph LR")
    assert "Sales PY" in out


TROUBLED = str(Path(__file__).resolve().parent.parent / "examples" / "troubled")


def test_check_reports_problems_and_exits_nonzero(capsys):
    assert main(["check", "-s", TROUBLED]) == 1
    out = capsys.readouterr().out
    assert "circular dependencies" in out
    assert "Ratio A -> Ratio B -> Ratio A" in out
    assert "unresolved references" in out
    assert "Refunds" in out and "Revenu" in out
    assert "Broken Measure: Error" in out


def test_build_survives_a_broken_model(tmp_path):
    out = tmp_path / "troubled.html"
    main(["build", "-s", TROUBLED, "-o", str(out)])
    assert "Ratio A" in out.read_text(encoding="utf-8")


SAMPLE_XLSX = str(Path(__file__).resolve().parent.parent / "docs" / "samples" / "info_sample.xlsx")
TROUBLED_XLSX = str(Path(__file__).resolve().parent.parent / "docs" / "samples" / "info_sample_with_problems.xlsx")


def test_reads_the_sample_workbook_like_the_csv_folder(capsys):
    assert main(["check", "-s", SAMPLE_XLSX]) == 0
    from_xlsx = capsys.readouterr().out.splitlines()[0]
    main(["check", "-s", EXAMPLES])
    from_csv = capsys.readouterr().out.splitlines()[0]
    assert from_xlsx == from_csv == "24 measures, 23 columns, 68 dependencies, deepest chain 5"


def test_workbook_with_problems(capsys):
    assert main(["check", "-s", TROUBLED_XLSX]) == 1
    assert "Ratio A -> Ratio B -> Ratio A" in capsys.readouterr().out
