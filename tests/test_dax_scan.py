from dax_lineage.dax import scan, tokenize

MEASURES = ["Total Sales", "Gross Margin", "Sales PY"]
COLUMNS = ["Amount", "Quantity", "Date", "Category"]
TABLES = ["Sales", "Date", "Product", "Order Lines"]


def refs(expression):
    result = scan(expression, MEASURES, COLUMNS, TABLES)
    return {(r.kind, r.label) for r in result.references}


def test_line_and_block_comments_are_ignored():
    expression = """
    // [Total Sales] in a line comment
    -- [Gross Margin] in a SQL style comment
    /* Sales[Amount] in a block comment */
    [Sales PY]
    """
    assert refs(expression) == {("measure", "[Sales PY]")}


def test_string_literals_are_ignored():
    assert refs('"[Total Sales] and Sales[Amount]" & [Sales PY]') == {("measure", "[Sales PY]")}


def test_escaped_quote_inside_string_does_not_leak():
    assert refs('"he said ""[Total Sales]""" & [Gross Margin]') == {("measure", "[Gross Margin]")}


def test_qualified_column_with_quoted_table():
    assert refs("SUM ( 'Order Lines'[Amount] )") == {
        ("column", "Order Lines[Amount]"),
        ("table", "Order Lines"),
    }


def test_bracket_escape_in_column_name():
    result = scan("SUM ( Sales[Amount ]] net] )", MEASURES, COLUMNS, TABLES)
    assert [r.name for r in result.columns] == ["Amount ] net"]


def test_bare_reference_resolves_to_measure_before_column():
    assert refs("[Total Sales] + [Quantity]") == {
        ("measure", "[Total Sales]"),
        ("column", "[Quantity]"),
    }


def test_unknown_bare_reference_is_reported_unresolved():
    result = scan("[No Such Thing] + 1", MEASURES, COLUMNS, TABLES)
    assert [r.name for r in result.unresolved] == ["No Such Thing"]


def test_variable_names_are_not_model_objects():
    expression = """
    VAR _total = [Total Sales]
    VAR _rows = FILTER ( Sales, Sales[Quantity] > 0 )
    RETURN
        SUMX ( _rows, _rows[Amount] ) + _total
    """
    result = scan(expression, MEASURES, COLUMNS, TABLES)
    assert result.variables == ["_total", "_rows"]
    assert ("table", "_rows") not in {(r.kind, r.label) for r in result.references}
    assert any(r.via_variable and r.name == "Amount" for r in result.columns)


def test_keyword_before_bracket_is_not_a_table():
    result = scan("VAR x = 1 RETURN [Total Sales]", MEASURES, COLUMNS, TABLES)
    assert [r.kind for r in result.references] == ["measure"]


def test_functions_and_flags():
    expression = """
    CALCULATE (
        AVERAGEX ( VALUES ( Product[Category] ), [Gross Margin] ),
        SAMEPERIODLASTYEAR ( 'Date'[Date] )
    )
    """
    result = scan(expression, MEASURES, COLUMNS, TABLES)
    assert result.time_intelligence == ["SAMEPERIODLASTYEAR"]
    assert result.iterators == ["AVERAGEX"]
    assert result.context_modifiers == ["CALCULATE"]
    assert "VALUES" in result.functions


def test_bare_table_reference_is_captured():
    assert ("table", "Sales") in refs("COUNTROWS ( ALL ( Sales ) )")


def test_measure_name_in_a_function_position_is_not_a_reference():
    # a function call must not be read as a table
    result = scan("DIVIDE ( [Total Sales], 2 )", MEASURES, COLUMNS, TABLES)
    assert {r.label for r in result.references} == {"[Total Sales]"}


def test_tokenizer_keeps_positions():
    tokens = tokenize("1 + [A]")
    assert [(t.kind, t.value) for t in tokens] == [
        ("number", "1"), ("punct", "+"), ("bracket", "A")
    ]
    assert tokens[-1].start == 4
