"""Turn the catalogue into a dependency graph with the numbers worth reading."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .dax import Reference, scan
from .model import Catalogue


def _norm(text: str) -> str:
    return (text or "").strip().casefold()


def measure_id(name: str) -> str:
    return f"m:{_norm(name)}"


def column_id(table: str, name: str) -> str:
    return f"c:{_norm(table)}[{_norm(name)}]"


def table_id(name: str) -> str:
    return f"t:{_norm(name)}"


@dataclass
class Node:
    id: str
    kind: str  # measure | column | table
    name: str
    table: str = ""
    label: str = ""
    expression: str = ""
    description: str = ""
    display_folder: str = ""
    format_string: str = ""
    data_type: str = ""
    is_hidden: bool = False
    is_calculated: bool = False
    state: str = ""
    error_message: str = ""
    functions: list = field(default_factory=list)
    time_intelligence: list = field(default_factory=list)
    iterators: list = field(default_factory=list)
    context_modifiers: list = field(default_factory=list)
    variables: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    depth: int = 0
    exists: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "table": self.table,
            "label": self.label or self.name,
            "expression": self.expression,
            "description": self.description,
            "displayFolder": self.display_folder,
            "formatString": self.format_string,
            "dataType": self.data_type,
            "isHidden": self.is_hidden,
            "isCalculated": self.is_calculated,
            "state": self.state,
            "errorMessage": self.error_message,
            "functions": sorted(set(self.functions)),
            "timeIntelligence": self.time_intelligence,
            "iterators": self.iterators,
            "contextModifiers": self.context_modifiers,
            "variables": self.variables,
            "unresolved": self.unresolved,
            "depth": self.depth,
            "exists": self.exists,
        }


@dataclass
class Edge:
    source: str  # the dependent
    target: str  # the dependency
    kind: str    # uses_measure | uses_column | uses_table | relationship
    via_variable: bool = False

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "viaVariable": self.via_variable,
        }


@dataclass
class Lineage:
    nodes: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)
    catalogue: Optional[Catalogue] = None
    cycles: list = field(default_factory=list)

    # -- traversal ----------------------------------------------------------

    def dependencies(self, node_id: str) -> list:
        """Everything ``node_id`` reads, directly."""
        return [e.target for e in self.edges if e.source == node_id]

    def dependents(self, node_id: str) -> list:
        """Everything that reads ``node_id``, directly."""
        return [e.source for e in self.edges if e.target == node_id]

    def _closure(self, node_id: str, step) -> list:
        seen, stack, out = {node_id}, [node_id], []
        while stack:
            current = stack.pop()
            for nxt in step(current):
                if nxt not in seen:
                    seen.add(nxt)
                    out.append(nxt)
                    stack.append(nxt)
        return out

    def upstream(self, node_id: str) -> list:
        return self._closure(node_id, self.dependencies)

    def downstream(self, node_id: str) -> list:
        return self._closure(node_id, self.dependents)

    # -- derived numbers ----------------------------------------------------

    def compute_depth(self) -> None:
        """Longest dependency chain under each node; cycles get a cut edge."""
        memo: dict = {}
        state: dict = {}

        def walk(node_id: str, path: list) -> int:
            if state.get(node_id) == "open":
                cycle = path[path.index(node_id):] + [node_id]
                if cycle not in self.cycles:
                    self.cycles.append(cycle)
                return 0
            if node_id in memo:
                return memo[node_id]
            state[node_id] = "open"
            best = 0
            for child in self.dependencies(node_id):
                best = max(best, 1 + walk(child, path + [node_id]))
            state[node_id] = "done"
            memo[node_id] = best
            return best

        for node_id in self.nodes:
            self.nodes[node_id].depth = walk(node_id, [])

    def stats(self) -> dict:
        measures = [n for n in self.nodes.values() if n.kind == "measure"]
        columns = [n for n in self.nodes.values() if n.kind == "column"]
        tables = [n for n in self.nodes.values() if n.kind == "table"]
        unresolved = sorted(
            {u for n in self.nodes.values() for u in n.unresolved}
        )
        leaf_measures = [
            m for m in measures
            if not any(
                self.nodes[d].kind == "measure"
                for d in self.dependencies(m.id)
                if d in self.nodes
            )
        ]
        unused = [
            m for m in measures
            if not any(
                self.nodes[d].kind == "measure" for d in self.dependents(m.id)
                if d in self.nodes
            )
        ]
        deepest = sorted(measures, key=lambda n: -n.depth)[:10]
        most_used = sorted(
            measures,
            key=lambda n: -len([d for d in self.dependents(n.id)]),
        )[:10]
        return {
            "measures": len(measures),
            "columns": len(columns),
            "tables": len(tables),
            "edges": len(self.edges),
            "missingObjects": len([n for n in self.nodes.values() if not n.exists]),
            "unresolvedReferences": unresolved,
            "cycles": self.cycles,
            "maxDepth": max([m.depth for m in measures], default=0),
            "baseMeasures": [m.name for m in leaf_measures],
            "unusedByOtherMeasures": [m.name for m in unused],
            "deepest": [{"name": m.name, "depth": m.depth} for m in deepest if m.depth],
            "mostReferenced": [
                {"name": m.name, "count": len(self.dependents(m.id))}
                for m in most_used
                if self.dependents(m.id)
            ],
        }

    def to_dict(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
            "stats": self.stats(),
            "tables": sorted(
                {n.table for n in self.nodes.values() if n.table}
                | {n.name for n in self.nodes.values() if n.kind == "table"}
            ),
            "folders": sorted(
                {n.display_folder for n in self.nodes.values() if n.display_folder}
            ),
        }


def build_graph(catalogue: Catalogue, include_relationships: bool = False) -> Lineage:
    """Scan every measure and calculated column and wire up the references."""
    lineage = Lineage(catalogue=catalogue)
    measure_names = catalogue.measure_names
    column_names = catalogue.column_names
    table_names = {t.key for t in catalogue.tables}

    # nodes for the objects that exist in the model
    for measure in catalogue.measures:
        lineage.nodes[measure_id(measure.name)] = Node(
            id=measure_id(measure.name),
            kind="measure",
            name=measure.name,
            table=measure.table_name,
            label=measure.name,
            expression=measure.expression,
            description=measure.description,
            display_folder=measure.display_folder,
            format_string=measure.format_string,
            data_type=measure.data_type,
            is_hidden=measure.is_hidden,
            state=measure.state,
            error_message=measure.error_message,
        )

    for column in catalogue.columns:
        node_id = column_id(column.table_name, column.name)
        lineage.nodes[node_id] = Node(
            id=node_id,
            kind="column",
            name=column.name,
            table=column.table_name,
            label=column.qualified_name,
            expression=column.expression,
            display_folder=column.display_folder,
            data_type=column.data_type,
            is_hidden=column.is_hidden,
            is_calculated=column.is_calculated,
        )

    def ensure_table(name: str) -> str:
        node_id = table_id(name)
        if node_id not in lineage.nodes:
            known = next((t for t in catalogue.tables if t.key == _norm(name)), None)
            lineage.nodes[node_id] = Node(
                id=node_id,
                kind="table",
                name=name,
                label=name,
                description=known.description if known else "",
                is_hidden=known.is_hidden if known else False,
                exists=known is not None or not catalogue.tables,
            )
        return node_id

    def ensure_measure(name: str) -> str:
        node_id = measure_id(name)
        if node_id not in lineage.nodes:
            lineage.nodes[node_id] = Node(
                id=node_id, kind="measure", name=name, label=name, exists=False
            )
        return node_id

    def ensure_column(table: str, name: str) -> str:
        resolved = catalogue.column(table, name)
        if resolved is not None:
            node_id = column_id(resolved.table_name, resolved.name)
            if node_id in lineage.nodes:
                return node_id
        node_id = column_id(table, name)
        if node_id not in lineage.nodes:
            lineage.nodes[node_id] = Node(
                id=node_id,
                kind="column",
                name=name,
                table=table,
                label=f"{table}[{name}]" if table else f"[{name}]",
                # only call it missing when there was a column catalogue to miss it from
                exists=not catalogue.columns,
            )
        return node_id

    seen_edges: set = set()

    def add_edge(source: str, target: str, kind: str, via_variable: bool = False) -> None:
        if source == target:
            return
        key = (source, target, kind)
        if key in seen_edges:
            return
        seen_edges.add(key)
        lineage.edges.append(Edge(source, target, kind, via_variable))

    def wire(node: Node, expression: str) -> None:
        result = scan(expression, measure_names, column_names, table_names)
        node.functions = sorted(set(result.functions))
        node.time_intelligence = result.time_intelligence
        node.iterators = result.iterators
        node.context_modifiers = result.context_modifiers
        node.variables = result.variables
        node.unresolved = sorted({r.name for r in result.unresolved})

        for ref in result.references:
            if ref.kind == "measure":
                add_edge(node.id, ensure_measure(ref.name), "uses_measure")
            elif ref.kind == "column":
                add_edge(
                    node.id,
                    ensure_column(ref.table, ref.name),
                    "uses_column",
                    ref.via_variable,
                )
            elif ref.kind == "table":
                add_edge(node.id, ensure_table(ref.name), "uses_table")
            elif ref.kind == "unresolved" and catalogue.columns:
                # measures and columns were both exported, so this name belongs
                # to neither: show it as a dangling measure reference
                add_edge(node.id, ensure_measure(ref.name), "uses_measure")

    for measure in catalogue.measures:
        wire(lineage.nodes[measure_id(measure.name)], measure.expression)

    for column in catalogue.columns:
        if column.is_calculated:
            wire(lineage.nodes[column_id(column.table_name, column.name)], column.expression)

    if include_relationships:
        for rel in catalogue.relationships:
            if rel.from_table and rel.to_table:
                add_edge(ensure_table(rel.from_table), ensure_table(rel.to_table), "relationship")

    lineage.compute_depth()
    return lineage
