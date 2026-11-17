"""Reports: every format gives the same usage, and the usage gives the right status."""
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

from dax_lineage.cli import main
from dax_lineage.graph import build_graph
from dax_lineage.load import load_workbook
from dax_lineage.pbi_report import collect_refs, decode, read_report

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_XLSX = ROOT / "docs" / "samples" / "info_sample.xlsx"


def _samples():
    spec = importlib.util.spec_from_file_location("make_report_samples", ROOT / "scripts" / "make_report_samples.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    target = tmp_path_factory.mktemp("reports")
    _samples().write_fixtures(target)
    return target


def key(u):
    return (u["page"], u["title"], u["visualType"], u["role"], u["kind"], u["table"], u["name"])


def test_every_format_gives_the_same_usage(fixtures, tmp_path):
    reference = sorted(map(key, read_report(fixtures / "sales_layout.json").usages))
    assert len(reference) == 27
    for name in ("sales.pbix", "sales.pbit", "sales_pbir.zip"):
        assert sorted(map(key, read_report(fixtures / name).usages)) == reference, name
    # the PBIR zip unpacked: a PBIP folder on disk
    with zipfile.ZipFile(fixtures / "sales_pbir.zip") as z:
        z.extractall(tmp_path / "project")
    assert sorted(map(key, read_report(tmp_path / "project").usages)) == reference


def test_roles_filters_formatting_and_hierarchies(fixtures):
    usages = set(map(key, read_report(fixtures / "sales.pbix").usages))
    assert ("Overview", "Orders over time", "lineChart", "Axis", "column", "Sales", "OrderDate") in usages
    assert ("(all pages)", "Report filters", "report filters", "Report filter", "column", "Date", "IsCurrentYear") in usages
    assert ("Customers", "Share by segment", "clusteredBarChart", "Conditional formatting", "measure",
            "Measures", "Gross Margin %") in usages
    assert ("Products", "Top products", "clusteredBarChart", "Visual filter", "measure", "Measures", "Total Sales") in usages
    assert ("Products", "Margin by category", "pivotTable", "Values", "column", "Sales", "Quantity") in usages


def test_pbix_needs_the_export_and_pbit_does_not(fixtures):
    pbix = read_report(fixtures / "sales.pbix")
    assert pbix.model is None and "binary" in pbix.note
    pbit = read_report(fixtures / "sales.pbit")
    assert len(pbit.model["measures"]) == 24


def test_statuses(fixtures):
    lineage = build_graph(load_workbook(SAMPLE_XLSX), usages=read_report(fixtures / "sales.pbix").usages)
    status = lambda node_id: lineage.nodes[node_id].usage["status"]
    assert status("m:total sales") == "visual"
    assert status("m:total cost") == "indirect"
    assert status("m:total quantity") == "deadChain"
    assert status("m:sales ytd") == "unused"
    assert status("c:date[monthnumber]") == "structural"
    assert status("c:product[productkey]") == "structural"
    assert status("c:sales[orderdate]") == "visual"
    assert status("c:date[quarter]") == "unused"
    report = lineage.stats()["report"]
    assert (report["visuals"], report["pages"]) == (10, 3)
    assert len(report["unusedMeasures"]) == 11
    # the report adds visuals, not model dependencies
    assert lineage.stats()["edges"] == 68


def test_utf16_layout_without_bom():
    text = json.dumps({"sections": []})
    assert decode(text.encode("utf-16-le")) == text
    assert decode(b"\xff\xfe" + text.encode("utf-16-le")) == text


def test_aliases_and_nested_json_strings():
    query = {"From": [{"Name": "s", "Entity": "Sales"}],
             "Select": [{"Measure": {"Expression": {"SourceRef": {"Source": "s"}}, "Property": "Total"}}]}
    assert collect_refs(json.dumps(query)) == [{"kind": "measure", "table": "Sales", "name": "Total"}]


def test_cli_unused_exits_one_and_lists(capsys, fixtures):
    assert main(["unused", "-s", str(SAMPLE_XLSX), "--report", str(fixtures / "sales.pbix"), "-o", "-"]) == 1
    out = capsys.readouterr().out
    assert "Sales YTD" in out and "Total Quantity" in out
    assert "Date[Quarter]" not in out
    main(["unused", "-s", str(SAMPLE_XLSX), "--report", str(fixtures / "sales.pbix"), "--columns-too", "-o", "-"])
    assert "Date[Quarter]" in capsys.readouterr().out


def test_cli_where_used(capsys, fixtures):
    main(["where-used", "Total Cost", "-s", str(fixtures / "sales.pbit"), "-o", "-"])
    out = capsys.readouterr().out
    assert "through other measures (3)" in out
    assert "Products > Margin by category  [pivotTable]  via Gross Margin" in out


def test_cli_pbix_alone_explains(fixtures):
    with pytest.raises(SystemExit, match="binary"):
        main(["unused", "-s", str(fixtures / "sales.pbix")])


def test_cli_unused_needs_a_report():
    with pytest.raises(SystemExit, match="needs the report"):
        main(["unused", "-s", str(SAMPLE_XLSX)])


def test_cli_check_flags_broken_visual_fields(capsys, fixtures):
    assert main(["check", "-s", str(fixtures / "problems.pbit")]) == 1
    assert "Summary › Against target: Sales Target" in capsys.readouterr().out


def test_cli_build_page_carries_usage(tmp_path, fixtures):
    out = tmp_path / "page.html"
    main(["build", "-s", str(SAMPLE_XLSX), "-r", str(fixtures / "sales.pbix"), "-o", str(out)])
    html = out.read_text(encoding="utf-8")
    assert '"unusedMeasures"' in html and '"kind":"visual"' in html
