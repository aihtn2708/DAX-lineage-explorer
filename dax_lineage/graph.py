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


def visual_id(report: str, visual: str) -> str:
    return f"v:{_norm(report)}|{_norm(visual)}"


def pretty_type(kind: str) -> str:
    text = str(kind or "visual")
    if text in ("page filters", "report filters"):
        return text[0].upper() + text[1:]
    import re
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return spaced[0].upper() + spaced[1:]


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
    report: str = ""
    page: str = ""
    page_order: int = 0
    visual_type: str = ""
    title: str = ""
    usage: Optional[dict] = None

    def to_dict(self) -> dict:
        out = {
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
        if self.kind == "visual":
            out.update({"report": self.report, "page": self.page, "pageOrder": self.page_order,
                        "visualType": self.visual_type, "title": self.title})
        if self.usage is not None:
            out["usage"] = self.usage
        return out


@dataclass
class Edge:
    source: str  # the dependent
    target: str  # the dependency
    kind: str    # uses_measure | uses_column | uses_table | relationship | used_in_visual
    via_variable: bool = False
    roles: list = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "viaVariable": self.via_variable,
        }
        if self.kind == "used_in_visual":
            out["roles"] = self.roles
        return out


@dataclass
class Lineage:
    nodes: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)
    catalogue: Optional[Catalogue] = None
    cycles: list = field(default_factory=list)
    has_report: bool = False

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

    def _field_dependents(self, node_id: str) -> list:
        return [d for d in self.dependents(node_id) if d in self.nodes and self.nodes[d].kind != "visual"]

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
            key=lambda n: -len(self._field_dependents(n.id)),
        )[:10]
        out = {
            "measures": len(measures),
            "columns": len(columns),
            "tables": len(tables),
            "edges": len([e for e in self.edges if e.kind != "used_in_visual"]),
            "missingObjects": len([n for n in self.nodes.values() if not n.exists]),
            "unresolvedReferences": unresolved,
            "cycles": self.cycles,
            "maxDepth": max([m.depth for m in measures], default=0),
            "baseMeasures": [m.name for m in leaf_measures],
            "unusedByOtherMeasures": [m.name for m in unused],
            "deepest": [{"name": m.name, "depth": m.depth} for m in deepest if m.depth],
            "mostReferenced": [
                {"name": m.name, "count": len(self._field_dependents(m.id))}
                for m in most_used
                if self._field_dependents(m.id)
            ],
        }
        if self.has_report:
            out["report"] = self.report_stats()
        return out

    def report_stats(self) -> dict:
        visuals = [n for n in self.nodes.values() if n.kind == "visual"]
        blank = {"visual": 0, "indirect": 0, "structural": 0, "deadChain": 0, "unused": 0}
        measures, columns = dict(blank), dict(blank)
        for n in self.nodes.values():
            if n.usage and n.exists:
                (measures if n.kind == "measure" else columns)[n.usage["status"]] += 1
        reports = []
        for v in visuals:
            if v.report not in reports:
                reports.append(v.report)
        return {
            "visuals": len([v for v in visuals if not v.visual_type.endswith("filters")]),
            "pages": len({v.page for v in visuals if v.page_order >= 0}),
            "reports": reports,
            "measures": measures,
            "columns": columns,
            "unusedMeasures": [n.name for n in self.nodes.values() if n.kind == "measure" and n.exists and n.usage
                               and n.usage["status"] in ("unused", "deadChain")],
            "brokenVisualRefs": [
                {"visual": self.nodes[e.source].label, "field": self.nodes[e.target].label}
                for e in self.edges
                if e.kind == "used_in_visual" and e.target in self.nodes and not self.nodes[e.target].exists
            ],
        }

    def unused(self) -> list:
        return [n for n in self.nodes.values() if n.usage and n.exists and n.usage["status"] in ("unused", "deadChain")]

    def to_dict(self) -> dict:
        fields = [n for n in self.nodes.values() if n.kind != "visual"]
        out = {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
            "stats": self.stats(),
            "tables": sorted(
                {n.table for n in fields if n.table}
                | {n.name for n in fields if n.kind == "table"}
            ),
            "folders": sorted(
                {n.display_folder for n in fields if n.display_folder}
            ),
        }
        if self.has_report:
            pages = []
            for n in self.nodes.values():
                if n.kind == "visual" and n.page not in pages:
                    pages.append(n.page)
            out["pages"] = pages
        return out


def build_graph(catalogue: Catalogue, include_relationships: bool = False, usages=None) -> Lineage:
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

    def ensure_measure(name: str, table: str = "") -> str:
        node_id = measure_id(name)
        if node_id not in lineage.nodes:
            lineage.nodes[node_id] = Node(
                id=node_id, kind="measure", name=name, label=name, table=table, exists=False
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
        edge_index[(source, target, kind)] = lineage.edges[-1]

    edge_index: dict = {}

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

    # report visuals: one node per visual (or filter pane), reading its fields
    for usage in usages or []:
        vid = visual_id(usage["report"], usage["visualId"])
        if vid not in lineage.nodes:
            title = usage.get("title") or pretty_type(usage.get("visualType"))
            lineage.nodes[vid] = Node(
                id=vid, kind="visual", name=title, table=usage["page"], label=f"{usage['page']} \u203a {title}",
                report=usage["report"], page=usage["page"], page_order=usage["pageOrder"],
                visual_type=usage.get("visualType") or "", title=usage.get("title") or "",
            )
        target = (ensure_measure(usage["name"], usage.get("table", "")) if usage["kind"] == "measure"
                  else ensure_column(usage.get("table", ""), usage["name"]))
        add_edge(vid, target, "used_in_visual")
        edge = edge_index.get((vid, target, "used_in_visual"))
        if edge is not None and usage["role"] not in edge.roles:
            edge.roles.append(usage["role"])

    lineage.compute_depth()
    deepest_field = max([n.depth for n in lineage.nodes.values() if n.kind != "visual"], default=0)
    for node in lineage.nodes.values():
        if node.kind == "visual":
            node.depth = deepest_field + 1
    if usages:
        lineage.has_report = True
        attach_usage(lineage, catalogue)
    return lineage


def attach_usage(lineage: Lineage, catalogue: Catalogue) -> None:
    """Status of every measure and column against the loaded report(s).

    visual      read by a visual or filter directly
    indirect    no visual reads it, but a measure that reaches a visual does
    structural  a column nothing reads, but a relationship key or sort-by target
    deadChain   read only by measures that are themselves unused
    unused      nothing reads it
    """
    readers = {node_id: [] for node_id in lineage.nodes}
    for edge in lineage.edges:
        if edge.target in readers:
            readers[edge.target].append(edge.source)
    is_visual = lambda i: i in lineage.nodes and lineage.nodes[i].kind == "visual"

    structural: dict = {}

    def mark(table, column, why):
        node_id = column_id(table, column)
        if node_id in lineage.nodes:
            structural.setdefault(node_id, []).append(why)

    for rel in catalogue.relationships:
        if rel.from_table and rel.from_column:
            mark(rel.from_table, rel.from_column, f"relationship key to {rel.to_table}")
        if rel.to_table and rel.to_column:
            mark(rel.to_table, rel.to_column, f"relationship key from {rel.from_table}")
    for column in catalogue.columns:
        if column.sort_by_column:
            mark(column.table_name, column.sort_by_column, f"sort-by column of {column.table_name}[{column.name}]")

    for node in lineage.nodes.values():
        if node.kind not in ("measure", "column"):
            continue
        direct = [r for r in readers[node.id] if is_visual(r)]
        via, seen = [], {node.id}
        queue = [r for r in readers[node.id] if not is_visual(r)]
        seen.update(queue)
        while queue:
            current = queue.pop(0)
            for reader in readers.get(current, []):
                if is_visual(reader):
                    if reader not in direct and not any(v["visual"] == reader for v in via):
                        via.append({"visual": reader, "through": current})
                elif reader not in seen:
                    seen.add(reader)
                    queue.append(reader)
        field_readers = [r for r in readers[node.id] if not is_visual(r)]
        why = structural.get(node.id, [])
        status = ("visual" if direct else "indirect" if via else "structural" if why
                  else "deadChain" if field_readers else "unused")
        node.usage = {"status": status, "visuals": direct, "via": via, "structural": why}
