"""Render the lineage graph as a single self-contained HTML page."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .graph import Lineage

TEMPLATE = Path(__file__).with_name("template.html")


def render_html(lineage: Lineage, title: str = "DAX measure lineage", source: str = "") -> str:
    payload = lineage.to_dict()
    stats = payload["stats"]
    subtitle_bits = [
        f"{stats['measures']} measures",
        f"{stats['columns']} columns",
        f"{stats['edges']} dependencies",
    ]
    if source:
        subtitle_bits.append(f"from {source}")
    subtitle_bits.append(datetime.now().strftime("read %Y-%m-%d %H:%M"))
    payload["meta"] = {"title": title, "subtitle": " · ".join(subtitle_bits), "source": source}

    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # keep the JSON safe inside a <script> element
    data = data.replace("</", "<\\/").replace("<!--", "<\\!--")

    html = TEMPLATE.read_text(encoding="utf-8")
    return html.replace("__TITLE__", _escape(title)).replace("__LINEAGE_DATA__", data)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def render_json(lineage: Lineage, indent: int = 2) -> str:
    return json.dumps(lineage.to_dict(), ensure_ascii=False, indent=indent)


def render_dot(lineage: Lineage) -> str:
    """Graphviz source, for a static picture or for feeding another tool."""
    style = {
        "measure": 'shape=box style="rounded,filled" fillcolor="#e6f0fc" color="#2a78d6"',
        "column": 'shape=box style=filled fillcolor="#fdeee7" color="#eb6834"',
        "table": 'shape=box style="rounded,filled" fillcolor="#e4f7f0" color="#1baf7a"',
    }
    lines = [
        "digraph lineage {",
        '  rankdir=LR;',
        '  graph [fontname="Helvetica", splines=spline, nodesep=0.22, ranksep=1.1];',
        '  node [fontname="Helvetica", fontsize=10, penwidth=1.4];',
        '  edge [color="#c3c2b7", arrowsize=0.7];',
    ]
    for node in lineage.nodes.values():
        extra = "" if node.exists else ' style="rounded,dashed" color="#d03b3b"'
        lines.append(
            f'  "{node.id}" [label="{node.label}" {style.get(node.kind, "")}{extra}];'
        )
    for edge in lineage.edges:
        dashed = ' [style=dashed]' if edge.kind == "relationship" else ""
        lines.append(f'  "{edge.target}" -> "{edge.source}"{dashed};')
    lines.append("}")
    return "\n".join(lines)


def render_mermaid(lineage: Lineage, node_ids=None) -> str:
    keep = set(node_ids) if node_ids else set(lineage.nodes)
    alias = {node_id: f"n{i}" for i, node_id in enumerate(sorted(keep))}
    wrap = {"measure": ("([", "])"), "table": ("[(", ")]"), "column": ("[", "]")}
    lines = ["graph LR"]
    for node_id in sorted(keep):
        node = lineage.nodes[node_id]
        open_, close = wrap.get(node.kind, ("[", "]"))
        label = node.label.replace('"', "'")
        lines.append(f'  {alias[node_id]}{open_}"{label}"{close}')
    for edge in lineage.edges:
        if edge.source in keep and edge.target in keep:
            lines.append(f"  {alias[edge.target]} --> {alias[edge.source]}")
    return "\n".join(lines)


def render_tree(lineage: Lineage, node_id: str, direction: str = "up", depth_limit: int = 12) -> str:
    """ASCII dependency tree, for the terminal."""
    step = lineage.dependencies if direction == "up" else lineage.dependents
    out: list = []

    def walk(current: str, prefix: str, is_last: bool, level: int, seen: frozenset) -> None:
        node = lineage.nodes.get(current)
        label = node.label if node else current
        tag = "" if not node else {"measure": "", "column": " (column)", "table": " (table)"}[node.kind]
        if level == 0:
            out.append(label + tag)
        else:
            out.append(f"{prefix}{'`-- ' if is_last else '|-- '}{label}{tag}")
        if current in seen:
            out[-1] += "  [cycle]"
            return
        if level >= depth_limit:
            return
        children = sorted(step(current), key=lambda i: lineage.nodes[i].label if i in lineage.nodes else i)
        child_prefix = prefix + ("    " if is_last else "|   ") if level else ""
        for index, child in enumerate(children):
            walk(child, child_prefix, index == len(children) - 1, level + 1, seen | {current})

    walk(node_id, "", True, 0, frozenset())
    return "\n".join(out)
