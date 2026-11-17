import json
from pathlib import Path

import pytest

from dax_lineage.load import autodiscover, load_catalogue, load_measures, read_rows

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_loads_the_sample_export():
    catalogue = load_catalogue(
        EXAMPLES / "info_measures.csv",
        EXAMPLES / "info_tables.csv",
        EXAMPLES / "info_columns.csv",
    )
    assert len(catalogue.measures) == 24
    assert catalogue.measure("total sales").expression.startswith("SUM")
    # TableID 5 is the Measures table
    assert catalogue.measure("Total Sales").table_name == "Measures"
    assert catalogue.column("Sales", "LineAmount").is_calculated


def test_header_variants_all_work(tmp_path):
    path = tmp_path / "export.csv"
    path.write_text(
        "Measures[Name],Measures[Expression],[TableID]\n"
        'Total Sales,"SUM ( Sales[Amount] )",1\n',
        encoding="utf-8",
    )
    measure = load_measures(path)[0]
    assert (measure.name, measure.table_id) == ("Total Sales", 1)


def test_tab_separated_with_bom(tmp_path):
    path = tmp_path / "export.tsv"
    path.write_text("﻿Name\tExpression\nTotal Sales\tSUM ( Sales[Amount] )\n", encoding="utf-8")
    assert load_measures(path)[0].name == "Total Sales"


def test_power_bi_rest_envelope(tmp_path):
    payload = {
        "results": [
            {"tables": [{"rows": [
                {"[Name]": "Total Sales", "[Expression]": "SUM ( Sales[Amount] )",
                 "[IsHidden]": False, "[TableID]": 2},
            ]}]}
        ]
    }
    path = tmp_path / "measures.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    measure = load_measures(path)[0]
    assert measure.name == "Total Sales"
    assert measure.is_hidden is False


def test_bare_json_list(tmp_path):
    path = tmp_path / "measures.json"
    path.write_text(json.dumps([{"Name": "M", "Expression": "1"}]), encoding="utf-8")
    assert load_measures(path)[0].name == "M"


def test_boolean_strings(tmp_path):
    path = tmp_path / "m.csv"
    path.write_text("Name,Expression,IsHidden\nA,1,TRUE\nB,2,False\n", encoding="utf-8")
    hidden = {m.name: m.is_hidden for m in load_measures(path)}
    assert hidden == {"A": True, "B": False}


def test_a_file_without_a_name_column_is_rejected(tmp_path):
    path = tmp_path / "wrong.csv"
    path.write_text("Foo,Bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no rows with a"):
        load_measures(path)


def test_autodiscover_finds_the_exports():
    found = autodiscover(EXAMPLES)
    assert found["measures_path"].name == "info_measures.csv"
    assert found["columns_path"].name == "info_columns.csv"
    assert found["tables_path"].name == "info_tables.csv"


def test_read_rows_normalises_headers(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("INFO.MEASURES()[Display Folder]\nCore\n", encoding="utf-8")
    assert read_rows(path) == [{"displayfolder": "Core"}]


def test_odd_workbook_shapes(tmp_path):
    """Inline strings, rich text, a title row, cells without refs, stored entries."""
    import subprocess, sys
    from dax_lineage.load import detect_kind, read_xlsx, rows_from_matrix

    target = tmp_path / "odd.xlsx"
    script = Path(__file__).resolve().parent / "js" / "make_odd_workbook.py"
    subprocess.run([sys.executable, str(script), str(target)], check=True)
    [(name, matrix)] = read_xlsx(target)
    rows = rows_from_matrix(matrix)
    assert name == "Result"
    assert [r["Name"] for r in rows] == ["Total Sales", "Margin"]
    assert rows[1]["Expression"] == "[Total Sales] * 0.3"
    assert rows[0]["IsHidden"] is True
    assert detect_kind(rows, name) == "measures"


def test_old_xls_is_refused_clearly(tmp_path):
    from dax_lineage.load import read_xlsx

    path = tmp_path / "old.xlsx"
    path.write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 64)
    with pytest.raises(ValueError, match="old .xls"):
        read_xlsx(path)
