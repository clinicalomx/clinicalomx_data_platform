"""Two DOT diagrams of one plan: the ROI containment tree it reads, and the object it assembles.

Both are drawn from the plan's own `RecipeReadiness` and `Relations`. Output is DOT text; no
`graphviz` package is needed.
"""
from __future__ import annotations

import html
import re

from clinicalomx.extract import is_inferred_cell
from clinicalomx.snapshot import type_name_of

#: Node fill per availability state. Colour is the state; shape is the kind.
STATE_COLOUR = {
    "present": ("#1a7f37", "#dafbe1"),
    "unplaceable": ("#9a6700", "#fff8c5"),
    "missing": ("#bc4c00", "#fff1e5"),
    "dangling": ("#bc4c00", "#fff1e5"),
    "denied": ("#cf222e", "#ffebe9"),
    "unlinked": ("#6e7781", "#f6f8fa"),
    "undeclared": ("#6e7781", "#f6f8fa"),
}
_UNREACHED = ("#afb8c1", "#ffffff")

KIND_SHAPE = {"image": "box", "labels": "box3d", "shapes": "hexagon",
              "table": "note", "points": "ellipse"}

#: Subtype glyphs for the source tree.
TYPE_ICON = {"WholeSlide": "\U0001f5c2", "TMA": "▦", "Core": "●",
             "TissueSection": "▱", "TileGrid": "⊞", "Tile": "▫"}


def _esc(text) -> str:
    return html.escape(str(text), quote=True)


def _node(name: str, label: str, *, shape: str, colours, extra: str = "") -> str:
    line, fill = colours
    return (f'  "{_esc(name)}" [label="{_esc(label)}", shape={shape}, style="filled,rounded", '
            f'color="{line}", fillcolor="{fill}", fontname="Helvetica", fontsize=10{extra}];')


def source_dot(availability, snapshot, *, recipe=None, max_nodes: int = 60) -> str:
    """The ROI containment tree under the selected ROI, badged with what each node carries.

    Nodes the recipe does not reach are greyed, not dropped.
    """
    root = snapshot.record_by_uid(availability.roi.uid)
    if root is None:
        return 'digraph G {label="the ROI Record has gone"; }'

    reached = set()
    if recipe is not None:
        reached = {r.uid for r in recipe.targets_under(root, snapshot)}
    def placeable(roi_type: str) -> bool:
        return recipe is None or recipe.placement_for(roi_type) is not None

    lines = ['digraph G {', '  rankdir=LR;', '  bgcolor="transparent";',
             '  node [margin="0.12,0.06"];', '  edge [color="#8c959f", arrowsize=0.6];']

    root_type = type_name_of(root) or "ROI"
    lines.append(_node(root.uid, f"{TYPE_ICON.get(root_type, '')} {root.name}".strip(),
                       shape="folder", colours=("#0969da", "#ddf4ff")))

    shown, truncated = 0, 0
    for record in snapshot.descendants(root):
        if shown >= max_nodes:
            truncated += 1
            continue
        shown += 1
        rtype = type_name_of(record) or "ROI"
        roles = sorted(snapshot.roles_of(record))
        badge = f"  [{len(roles)}]" if roles else ""
        label = f"{TYPE_ICON.get(rtype, '')} {str(record.name).rsplit(':', 1)[-1]}{badge}"

        if is_inferred_cell(snapshot.values(record)):
            # Inferred cells are excluded from counts but still drawn.
            colours, extra = _UNREACHED, ', fontcolor="#8c959f", style="filled,dashed"'
            label += "\\ninferred"
        elif record.uid not in reached:
            colours, extra = _UNREACHED, ', fontcolor="#8c959f"'
        elif not placeable(rtype):
            # Walked and refused: no placement is declared for this subtype.
            colours, extra = STATE_COLOUR["unplaceable"], ""
            label += "\\nno placement"
        else:
            colours, extra = (STATE_COLOUR["present"] if roles
                              else STATE_COLOUR["unlinked"]), ""
        lines.append(_node(record.uid, label, shape="box", colours=colours, extra=extra))
        parent = snapshot.parent(record)
        if parent is not None:
            lines.append(f'  "{_esc(parent.uid)}" -> "{_esc(record.uid)}";')

    if truncated:
        lines.append(_node("__more__", f"... and {truncated} more",
                           shape="plaintext", colours=("#ffffff", "#ffffff")))
        lines.append(f'  "{_esc(root.uid)}" -> "__more__" [style=dotted];')
    lines.append("}")
    return "\n".join(lines)


def _contributors(element, *, limit: int) -> tuple[list[tuple[str, str]], int]:
    """``([(child roi_id, role)], how many more)`` for a per-child element, from the plan."""
    if element.level != "children" or not element.per_target_role:
        return [], 0
    items = sorted(element.per_target_role.items())
    shown = [(name.rsplit(":", 1)[-1], role) for name, role in items[:limit]]
    return shown, max(0, len(items) - limit)


#: How contributing ROIs are grouped in the object graph: ``roi`` (one box per ROI) or
#: ``element`` (one row per slot).
GROUPING = ("roi", "element")


def object_dot(readiness, relations=None, *, max_per_slot: int = 8,
               aggregates: bool = False, max_sources: int = 2,
               group: str = "roi") -> str:
    """The object being assembled: one cluster per model slot, with edges into and between them.

    Fan-in edges show where a slot's contents come from; with ``aggregates`` many children feed
    one node (a mosaic). Relation edges link finished elements; an unresolved derivation is a red
    dashed stub.
    """
    lines = ['digraph G {', '  rankdir=LR;', '  bgcolor="transparent";', '  compound=true;',
             '  node [margin="0.12,0.06"];',
             '  graph [fontname="Helvetica", fontsize=10, color="#d0d7de"];']

    by_kind: dict[str, list] = {}
    for element in readiness.elements:
        by_kind.setdefault(element.kind, []).append(element)

    fan_in = _sources_by_roi if group == "roi" else _source_cluster
    lines.extend(fan_in(by_kind, aggregates=aggregates, max_sources=max_sources,
                        max_per_slot=max_per_slot))

    for index, (kind, elements) in enumerate(sorted(by_kind.items())):
        lines.append(f'  subgraph cluster_{index} {{')
        lines.append(f'    label="{_esc(kind)}"; style=rounded;')
        for element in elements[:max_per_slot]:
            role = element.chosen_role or "-"
            if element.variable:
                role = f"{role}#{element.variable}"
            colours = STATE_COLOUR.get(element.state, _UNREACHED)
            label = f"{element.element}\\n{role}"
            # A mosaic is one node; a by-core merge has one element per child.
            fan = len(element.per_target_role or {})
            if element.level == "children" and fan:
                label += f"\\n{'mosaic of ' + str(fan) if aggregates else f'×{fan}, one per ROI'}"
            lines.append("  " + _node(element.element, label,
                                      shape=KIND_SHAPE.get(kind, "box"), colours=colours))
        if len(elements) > max_per_slot:
            lines.append(f'    "more_{index}" [label="... and '
                         f'{len(elements) - max_per_slot} more", shape=plaintext, '
                         f'fontname="Helvetica", fontsize=9];')
        lines.append("  }")

    known = {e.element for e in readiness.elements}
    for n, edge in enumerate(relations.edges if relations is not None else ()):
        if edge.target not in known:
            continue
        if edge.source in known:
            style = "solid" if edge.kind == "derived_from" else "dashed"
            label = "derived from" if edge.kind == "derived_from" else "annotates"
            lines.append(f'  "{_esc(edge.source)}" -> "{_esc(edge.target)}" '
                         f'[label="{label}", style={style}, color="#0969da", '
                         f'fontname="Helvetica", fontsize=9, fontcolor="#0969da"];')
        else:
            # An unresolved derivation, drawn as a stub.
            gap = f"__gap{n}__"
            text = ("? ambiguous\\n" + "\\nor ".join(edge.candidates)) if edge.candidates else "?"
            lines.append(f'  "{gap}" [label="{_esc(text)}", shape=octagon, style="filled", '
                         f'color="#cf222e", fillcolor="#ffebe9", fontname="Helvetica", '
                         f'fontsize=9];')
            lines.append(f'  "{gap}" -> "{_esc(edge.target)}" [style=dashed, color="#cf222e", '
                         f'label="source unknown", fontname="Helvetica", fontsize=9, '
                         f'fontcolor="#cf222e"];')
    lines.append("}")
    return "\n".join(lines)


def _natural(name: str):
    """Natural sort key: `R0C2` before `R0C10`."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)]


def _sources_by_roi(by_kind, *, aggregates: bool, max_sources: int, max_per_slot: int):
    """One box per contributing ROI, holding the roles it carries and where each lands."""
    contributions: dict[str, list] = {}
    for elements in by_kind.values():
        for element in elements[:max_per_slot]:
            for roi_name, role in (element.per_target_role or {}).items():
                label = f"{role}#{element.variable}" if element.variable else role
                contributions.setdefault(roi_name, []).append((element, label))
    if not contributions:
        return []

    ordered = sorted(contributions, key=lambda n: _natural(n.rsplit(":", 1)[-1]))
    out, edges = [], []
    for roi_name in ordered[:max_sources]:
        roi_id = roi_name.rsplit(":", 1)[-1]
        out.append(f'  subgraph "cluster_roi_{_esc(roi_id)}" {{')
        out.append(f'    label="{_esc(roi_id)}"; style="rounded,filled"; '
                   f'fillcolor="#f6f8fa"; color="#d0d7de";')
        for element, label in contributions[roi_name]:
            node = f"src_{roi_id}_{element.element}"
            out.append("  " + _node(node, label, shape=KIND_SHAPE.get(element.kind, "box"),
                                    colours=STATE_COLOUR["present"], extra=", fontsize=9"))
            tail = ' [label="composited", fontname="Helvetica", fontsize=8, color="#0969da"]' \
                if aggregates else ' [color="#8c959f"]'
            edges.append(f'  "{_esc(node)}" -> "{_esc(element.element)}"{tail};')
        out.append("  }")

    if len(ordered) > max_sources:
        out.append(_node("__more_rois__", f"... and {len(ordered) - max_sources} more ROI(s)",
                         shape="plaintext", colours=("#ffffff", "#ffffff"), extra=", fontsize=9"))
    return out + edges


def _source_cluster(by_kind, *, aggregates: bool, max_sources: int, max_per_slot: int):
    """The per-child contributors, drawn as ``roi_id: role``, with an edge into each slot."""
    per_child = [e for elements in by_kind.values() for e in elements[:max_per_slot]
                 if e.level == "children" and e.per_target_role]
    if not per_child:
        return []

    out = ['  subgraph cluster_src {',
           f'    label="contributing ROIs{" (composited)" if aggregates else ""}"; '
           f'style=rounded; color="#d0d7de";']
    edges = []
    for element in per_child:
        shown, more = _contributors(element, limit=max_sources)
        for roi_id, role in shown:
            node = f"src_{element.element}_{roi_id}"
            label = f"{roi_id}: {role}"
            if element.variable:
                label += f"#{element.variable}"
            out.append("  " + _node(node, label, shape="box",
                                    colours=STATE_COLOUR["present"],
                                    extra=", fontsize=9"))
            # Label converging edges so a mosaic reads as compositing.
            tail = ' [label="composited", fontname="Helvetica", fontsize=8, color="#0969da"]' \
                if aggregates else ' [color="#8c959f"]'
            edges.append(f'  "{_esc(node)}" -> "{_esc(element.element)}"{tail};')
        if more:
            node = f"src_{element.element}_more"
            out.append("  " + _node(node, f"... and {more} more", shape="plaintext",
                                    colours=("#ffffff", "#ffffff"), extra=", fontsize=9"))
            edges.append(f'  "{_esc(node)}" -> "{_esc(element.element)}" '
                         f'[style=dotted, color="#8c959f"];')
    out.append("  }")
    return out + edges


def legend() -> str:
    """The legend: colour is the state, shape is the kind."""
    return ("🟩 present · 🟨 unplaceable / no placement declared · 🟥 denied · "
            "⬜ unlinked or not reached by this recipe · dashed = inferred grid cell, not a "
            "core  ·  "
            "grey edge = contributes to, solid blue = derived from, dashed = annotates, "
            "red = nothing recorded it")
