"""A small DAX scanner, used only to find what an expression depends on.

This is deliberately not a parser. It tokenises far enough to know what is a
comment, a string, a quoted table name and a bracketed reference, and then
applies the resolution rule the engine itself uses: a bare ``[Ref]`` is a
measure if a measure of that name exists, otherwise a column of the table in
the current row context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# Words that may sit directly in front of a bracket without being a table.
KEYWORDS = {
    "var", "return", "define", "evaluate", "measure", "column", "table",
    "order", "by", "start", "at", "asc", "desc", "in", "not", "and", "or",
    "true", "false", "then", "else", "datatable", "with",
}

TIME_INTELLIGENCE = {
    "dateadd", "datesbetween", "datesinperiod", "datesmtd", "datesqtd",
    "datesytd", "endofmonth", "endofquarter", "endofyear", "firstdate",
    "firstnonblank", "lastdate", "lastnonblank", "nextday", "nextmonth",
    "nextquarter", "nextyear", "openingbalancemonth", "openingbalancequarter",
    "openingbalanceyear", "closingbalancemonth", "closingbalancequarter",
    "closingbalanceyear", "parallelperiod", "previousday", "previousmonth",
    "previousquarter", "previousyear", "sameperiodlastyear", "startofmonth",
    "startofquarter", "startofyear", "totalmtd", "totalqtd", "totalytd",
}

CONTEXT_MODIFIERS = {
    "calculate", "calculatetable", "all", "allexcept", "allselected",
    "allnoblankrow", "allcrossfiltered", "removefilters", "keepfilters",
    "userelationship", "crossfilter", "treatas",
}

ITERATORS = {
    "sumx", "averagex", "minx", "maxx", "countx", "countax", "concatenatex",
    "productx", "medianx", "percentilex.inc", "percentilex.exc", "rankx",
    "stdevx.p", "stdevx.s", "varx.p", "varx.s", "generate", "generateall",
    "addcolumns", "selectcolumns", "filter", "topn",
}

_IDENT_START = re.compile(r"[A-Za-z_À-￿]")
_IDENT_BODY = re.compile(r"[A-Za-z0-9_.À-￿]")


@dataclass
class Token:
    kind: str  # comment | string | table | bracket | ident | number | punct
    value: str
    start: int
    end: int


def tokenize(text: str) -> list:
    """Split a DAX expression into tokens. Whitespace is dropped."""
    tokens: list = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]

        if ch in " \t\r\n\f\v":
            i += 1
            continue

        # line comments: // and --
        if text.startswith("//", i) or text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            tokens.append(Token("comment", text[i:j], i, j))
            i = j
            continue

        # block comment
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            tokens.append(Token("comment", text[i:j], i, j))
            i = j
            continue

        # string literal, "" escapes a quote
        if ch == '"':
            j, buf = i + 1, []
            while j < n:
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    j += 1
                    break
                buf.append(text[j])
                j += 1
            tokens.append(Token("string", "".join(buf), i, j))
            i = j
            continue

        # quoted table name, '' escapes a quote
        if ch == "'":
            j, buf = i + 1, []
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    j += 1
                    break
                buf.append(text[j])
                j += 1
            tokens.append(Token("table", "".join(buf), i, j))
            i = j
            continue

        # bracketed name, ]] escapes a bracket
        if ch == "[":
            j, buf = i + 1, []
            while j < n:
                if text[j] == "]":
                    if j + 1 < n and text[j + 1] == "]":
                        buf.append("]")
                        j += 2
                        continue
                    j += 1
                    break
                buf.append(text[j])
                j += 1
            tokens.append(Token("bracket", "".join(buf), i, j))
            i = j
            continue

        if ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isdigit() or text[j] in ".eE" or
                             (text[j] in "+-" and j > i and text[j - 1] in "eE")):
                j += 1
            tokens.append(Token("number", text[i:j], i, j))
            i = j
            continue

        if _IDENT_START.match(ch):
            j = i + 1
            while j < n and _IDENT_BODY.match(text[j]):
                j += 1
            # a trailing dot belongs to the next thing, not the name
            while j > i + 1 and text[j - 1] == ".":
                j -= 1
            tokens.append(Token("ident", text[i:j], i, j))
            i = j
            continue

        tokens.append(Token("punct", ch, i, i + 1))
        i += 1
    return tokens


@dataclass(frozen=True)
class Reference:
    kind: str  # measure | column | table | unresolved
    name: str
    table: str = ""
    via_variable: bool = False
    position: int = 0

    @property
    def label(self) -> str:
        if self.kind == "table":
            return self.name
        return f"{self.table}[{self.name}]" if self.table else f"[{self.name}]"


@dataclass
class Scan:
    references: list = field(default_factory=list)
    functions: list = field(default_factory=list)
    variables: list = field(default_factory=list)

    @property
    def measures(self) -> list:
        return [r for r in self.references if r.kind == "measure"]

    @property
    def columns(self) -> list:
        return [r for r in self.references if r.kind == "column"]

    @property
    def tables(self) -> list:
        return [r for r in self.references if r.kind == "table"]

    @property
    def unresolved(self) -> list:
        return [r for r in self.references if r.kind == "unresolved"]

    @property
    def time_intelligence(self) -> list:
        return sorted({f for f in self.functions if f.casefold() in TIME_INTELLIGENCE})

    @property
    def context_modifiers(self) -> list:
        return sorted({f for f in self.functions if f.casefold() in CONTEXT_MODIFIERS})

    @property
    def iterators(self) -> list:
        return sorted({f for f in self.functions if f.casefold() in ITERATORS})


def _norm(text: str) -> str:
    return (text or "").strip().casefold()


def declared_variables(tokens: Iterable) -> list:
    """Names introduced by VAR, which are not model objects."""
    names, previous = [], None
    for token in tokens:
        if token.kind == "comment":
            continue
        if previous is not None and previous == "var" and token.kind == "ident":
            names.append(token.value)
        previous = _norm(token.value) if token.kind == "ident" else None
    return names


def scan(
    expression: str,
    measure_names: Optional[Iterable] = None,
    column_names: Optional[Iterable] = None,
    table_names: Optional[Iterable] = None,
) -> Scan:
    """Find every model object an expression refers to.

    ``measure_names`` and ``column_names`` are bare names; they decide how an
    unqualified ``[Ref]`` is read. Without them every unqualified reference is
    reported as ``unresolved``, which is why the tool asks for INFO.MEASURES
    and INFO.COLUMNS together.
    """
    measures = {_norm(x) for x in (measure_names or [])}
    columns = {_norm(x) for x in (column_names or [])}
    tables = {_norm(x) for x in (table_names or [])}

    tokens = [t for t in tokenize(expression or "") if t.kind != "comment"]
    variables = declared_variables(tokens)
    var_set = {_norm(v) for v in variables}

    result = Scan(variables=variables)
    seen: set = set()

    def add(ref: Reference) -> None:
        key = (ref.kind, _norm(ref.table), _norm(ref.name))
        if key in seen:
            return
        seen.add(key)
        result.references.append(ref)

    for index, token in enumerate(tokens):
        nxt = tokens[index + 1] if index + 1 < len(tokens) else None
        prev = tokens[index - 1] if index else None

        if token.kind == "ident":
            if nxt is not None and nxt.kind == "punct" and nxt.value == "(":
                if _norm(token.value) not in KEYWORDS:
                    result.functions.append(token.value)
                continue
            # a bare table name, as in ALL(Sales) or SUMX(Sales, ...)
            if (nxt is None or nxt.kind != "bracket") and _norm(token.value) in tables:
                add(Reference("table", token.value, position=token.start))
            continue

        if token.kind == "table":
            # 'Table'[Col] is handled when the bracket is reached
            if nxt is None or nxt.kind != "bracket":
                add(Reference("table", token.value, position=token.start))
            continue

        if token.kind != "bracket":
            continue

        name = token.value

        # qualified: Table[Col] or 'Table'[Col] or var[Col]
        if prev is not None and prev.kind in {"ident", "table"}:
            owner = prev.value
            if prev.kind == "table" or _norm(owner) not in KEYWORDS:
                if _norm(owner) in var_set:
                    add(Reference("column", name, "", True, token.start))
                else:
                    add(Reference("column", name, owner, position=token.start))
                    if _norm(owner) in tables or prev.kind == "table":
                        add(Reference("table", owner, position=prev.start))
                continue

        # unqualified: the engine's own resolution order
        if _norm(name) in measures:
            add(Reference("measure", name, position=token.start))
        elif _norm(name) in columns:
            add(Reference("column", name, position=token.start))
        else:
            add(Reference("unresolved", name, position=token.start))

    return result
