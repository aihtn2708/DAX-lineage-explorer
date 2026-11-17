"""Objects that mirror the rows returned by the INFO.* DAX functions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


def _norm(name: Optional[str]) -> str:
    """Case-insensitive key for a model object name."""
    return (name or "").strip().casefold()


@dataclass
class Table:
    id: Optional[int]
    name: str
    description: str = ""
    is_hidden: bool = False
    lineage_tag: str = ""

    @property
    def key(self) -> str:
        return _norm(self.name)


@dataclass
class Column:
    id: Optional[int]
    table_id: Optional[int]
    name: str
    table_name: str = ""
    expression: str = ""
    data_type: str = ""
    column_type: str = ""
    is_hidden: bool = False
    display_folder: str = ""
    lineage_tag: str = ""
    sort_by_column_id: Optional[int] = None
    sort_by_column: str = ""

    @property
    def is_calculated(self) -> bool:
        return bool(self.expression and self.expression.strip())

    @property
    def qualified_name(self) -> str:
        return f"{self.table_name}[{self.name}]" if self.table_name else f"[{self.name}]"

    @property
    def key(self) -> str:
        return f"{_norm(self.table_name)}[{_norm(self.name)}]"


@dataclass
class Measure:
    id: Optional[int]
    table_id: Optional[int]
    name: str
    expression: str
    table_name: str = ""
    description: str = ""
    format_string: str = ""
    display_folder: str = ""
    is_hidden: bool = False
    state: str = ""
    error_message: str = ""
    data_type: str = ""
    modified_time: str = ""
    lineage_tag: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        return f"{self.table_name}[{self.name}]" if self.table_name else f"[{self.name}]"

    @property
    def key(self) -> str:
        return _norm(self.name)


@dataclass
class Relationship:
    id: Optional[int]
    from_table_id: Optional[int]
    from_column_id: Optional[int]
    to_table_id: Optional[int]
    to_column_id: Optional[int]
    is_active: bool = True
    from_table: str = ""
    to_table: str = ""
    from_column: str = ""
    to_column: str = ""
    cross_filtering: str = ""


@dataclass
class Catalogue:
    """Everything loaded from the queries, with the lookups the parser needs."""

    measures: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    relationships: list = field(default_factory=list)

    def resolve(self) -> "Catalogue":
        """Fill table names on measures and columns from TableID, and vice versa."""
        by_id = {t.id: t.name for t in self.tables if t.id is not None}
        by_name = {t.key: t.id for t in self.tables}

        for item in list(self.measures) + list(self.columns):
            if not item.table_name and item.table_id in by_id:
                item.table_name = by_id[item.table_id]
            if item.table_id is None and item.table_name:
                item.table_id = by_name.get(_norm(item.table_name))

        # Tables that only ever appear via a measure or column reference.
        known = {t.key for t in self.tables}
        for item in list(self.measures) + list(self.columns):
            if item.table_name and _norm(item.table_name) not in known:
                self.tables.append(Table(id=item.table_id, name=item.table_name))
                known.add(_norm(item.table_name))

        cols_by_id = {c.id: c for c in self.columns if c.id is not None}
        for rel in self.relationships:
            fc, tc = cols_by_id.get(rel.from_column_id), cols_by_id.get(rel.to_column_id)
            if fc is not None:
                rel.from_column = rel.from_column or fc.name
                rel.from_table = rel.from_table or fc.table_name or by_id.get(fc.table_id, "")
            if tc is not None:
                rel.to_column = rel.to_column or tc.name
                rel.to_table = rel.to_table or tc.table_name or by_id.get(tc.table_id, "")
            rel.from_table = rel.from_table or by_id.get(rel.from_table_id, "")
            rel.to_table = rel.to_table or by_id.get(rel.to_table_id, "")
        for column in self.columns:
            target = cols_by_id.get(column.sort_by_column_id) if column.sort_by_column_id is not None else None
            if target is not None:
                column.sort_by_column = target.name
        return self

    # -- lookups used while extracting references ---------------------------

    @property
    def measure_names(self) -> set:
        return {m.key for m in self.measures}

    @property
    def column_names(self) -> set:
        """Bare column names, for deciding what an unqualified [Ref] means."""
        return {_norm(c.name) for c in self.columns}

    def measure(self, name: str):
        key = _norm(name)
        for m in self.measures:
            if m.key == key:
                return m
        return None

    def column(self, table: str, name: str):
        want_table, want_col = _norm(table), _norm(name)
        hits = [c for c in self.columns if _norm(c.name) == want_col]
        if want_table:
            for c in hits:
                if _norm(c.table_name) == want_table:
                    return c
            return None
        return hits[0] if len(hits) == 1 else None
