"""Render the transform tree — diagnosed, not merely drawn.

`ros2 run tf2_tools view_frames` produces a PDF of the tree and leaves you to work out
what is wrong with it. That is the wrong division of labour: the tool has every number it
needs to say *which* edge is broken and *how*, and a human squinting at a graph is slower
and less reliable at it than a rule is.

So this renders the same tree, with the diagnosis already applied:

* every edge is coloured by its own health, and labelled with its rate and how much of
  the recording it actually existed for;
* a frame a sensor publishes in but that no transform provides is drawn as a **dangling**
  node, because that is exactly what it is;
* the findings are printed beside the graph, so the picture and the reason are on one page.

Three output formats, all dependency-free — no graphviz, no cairo:

* **`.pdf`** — what `view_frames` gives you, but with the answer on it
* **`.svg`** — the same page, viewable and printable anywhere
* **`.dot`** — for anyone who does have graphviz and wants their own layout

The layout is a layered tree walk rather than a force-directed graph. TF *is* a tree —
each frame has exactly one parent — so a layered layout is both correct and stable, which
matters when comparing two runs of the same robot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .models import Finding, TransformReport

#: severity colours, as (r, g, b) in 0..1
_OK = (0.30, 0.34, 0.40)
_BAD = (0.83, 0.18, 0.18)
_WARN = (0.87, 0.55, 0.10)
_STATIC = (0.20, 0.45, 0.75)
_DANGLING = (0.60, 0.20, 0.60)

NODE_W, NODE_H = 150.0, 28.0
COL_GAP, ROW_GAP = 90.0, 46.0
MARGIN = 40.0


@dataclass
class _Node:
    name: str
    depth: int = 0
    row: int = 0
    dangling: bool = False
    #: the topic that publishes in this frame, when nothing provides it
    orphan_topic: str = ""
    x: float = 0.0
    y: float = 0.0


@dataclass
class Layout:
    nodes: dict[str, _Node] = field(default_factory=dict)
    #: (parent, child, colour, label, dashed)
    edges: list[tuple[str, str, tuple[float, float, float], str, bool]] = field(
        default_factory=list
    )
    width: float = 0.0
    height: float = 0.0


def _edge_colour(e) -> tuple[tuple[float, float, float], str]:
    """Colour and one-line label for a transform, from its own measurements."""
    bits = []
    colour = _STATIC if e.static else _OK
    if e.static:
        bits.append("static")
    else:
        bits.append(f"{e.hz:.0f} Hz")
    if not e.static and e.present_fraction < 0.95:
        colour = _BAD
        bits.append(f"present {e.present_fraction:.0%}")
    if e.disagreements:
        colour = _BAD
        bits.append(f"2 publishers ±{e.max_disagreement_m:.2f} m")
    if e.mean_stamp_lag_ms < 0:
        colour = _WARN
        bits.append(f"stamped {-e.mean_stamp_lag_ms:.0f} ms ahead")
    elif e.max_gap_s > 1.0:
        colour = _WARN if colour is _OK else colour
        bits.append(f"gap {e.max_gap_s:.1f}s")
    return colour, "  ".join(bits)


def layout(report: TransformReport) -> Layout:
    """Layered tree layout. Deterministic, so two runs of one robot line up."""
    lay = Layout()
    children: dict[str, list[str]] = {}
    for e in report.edges:
        children.setdefault(e.parent, []).append(e.child)
        lay.nodes.setdefault(e.parent, _Node(e.parent))
        lay.nodes.setdefault(e.child, _Node(e.child))

    # frames a sensor uses that the tree does not provide: drawn, because their absence
    # is the finding and an absent node is impossible to notice
    for frame, topic in report.orphan_frames.items():
        node = lay.nodes.setdefault(frame, _Node(frame))
        node.dangling = True
        node.orphan_topic = topic

    roots = list(report.roots) or sorted(
        n for n in lay.nodes if all(n != e.child for e in report.edges)
    )
    seen: set[str] = set()
    row = 0

    def walk(name: str, depth: int) -> None:
        nonlocal row
        if name in seen:
            return
        seen.add(name)
        node = lay.nodes[name]
        node.depth, node.row = depth, row
        row += 1
        for child in sorted(children.get(name, [])):
            walk(child, depth + 1)

    for r in sorted(roots):
        if r in lay.nodes:
            walk(r, 0)
    for name in sorted(lay.nodes):  # anything disconnected still gets a place
        walk(name, 0)

    for node in lay.nodes.values():
        node.x = MARGIN + node.depth * (NODE_W + COL_GAP)
        node.y = MARGIN + node.row * ROW_GAP

    for e in report.edges:
        colour, label = _edge_colour(e)
        lay.edges.append((e.parent, e.child, colour, label, e.static))

    lay.width = max((n.x + NODE_W for n in lay.nodes.values()), default=200.0) + MARGIN
    lay.height = max((n.y + NODE_H for n in lay.nodes.values()), default=100.0) + MARGIN
    return lay


# ----------------------------------------------------------------------------- dot


def to_dot(report: TransformReport, findings: list[Finding] | None = None) -> str:
    out = ["digraph tf {", "  rankdir=LR;", '  node [shape=box, fontname="Helvetica"];',
           '  edge [fontname="Helvetica", fontsize=9];']
    for frame, topic in report.orphan_frames.items():
        out.append(f'  "{frame}" [color="#992099", style=dashed, '
                   f'label="{frame}\\nno transform ({topic})"];')
    for e in report.edges:
        colour, label = _edge_colour(e)
        r, g, b = (int(c * 255) for c in colour)
        hexcol = f"#{r:02x}{g:02x}{b:02x}"
        style = ", style=dashed" if e.static else ""
        out.append(f'  "{e.parent}" -> "{e.child}" [color="{hexcol}", '
                   f'label="{label}"{style}];')
    # findings are reported alongside the graph rather than inside it: a DOT file is
    # someone else's input, and burying prose in node labels makes it worse input
    out.append("}")
    return "\n".join(out)


# ----------------------------------------------------------------------------- svg


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def to_svg(report: TransformReport, findings: list[Finding] | None = None) -> str:
    lay = layout(report)
    findings = findings or []
    panel = 30.0 + 16.0 * (len(findings) + 2) if findings else 0.0
    height = lay.height + panel

    def rgb(c: tuple[float, float, float]) -> str:
        r, g, b = (int(v * 255) for v in c)
        return f"rgb({r},{g},{b})"

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{lay.width:.0f}" '
        f'height="{height:.0f}" viewBox="0 0 {lay.width:.0f} {height:.0f}" '
        f'font-family="Helvetica,Arial,sans-serif">',
        '<defs><marker id="a" markerWidth="9" markerHeight="7" refX="9" refY="3.5" '
        'orient="auto"><polygon points="0 0, 9 3.5, 0 7" fill="context-stroke"/>'
        "</marker></defs>",
        f'<rect width="{lay.width:.0f}" height="{height:.0f}" fill="white"/>',
    ]

    for parent, child, colour, label, dashed in lay.edges:
        a, b = lay.nodes[parent], lay.nodes[child]
        x1, y1 = a.x + NODE_W, a.y + NODE_H / 2
        x2, y2 = b.x, b.y + NODE_H / 2
        mx = (x1 + x2) / 2
        dash = ' stroke-dasharray="5,4"' if dashed else ""
        parts.append(
            f'<path d="M{x1:.1f},{y1:.1f} C{mx:.1f},{y1:.1f} {mx:.1f},{y2:.1f} '
            f'{x2:.1f},{y2:.1f}" fill="none" stroke="{rgb(colour)}" stroke-width="1.6"'
            f'{dash} marker-end="url(#a)"/>'
        )
        if label:
            parts.append(
                f'<text x="{mx:.1f}" y="{(y1 + y2) / 2 - 4:.1f}" font-size="9" '
                f'fill="{rgb(colour)}" text-anchor="middle">{_esc(label)}</text>'
            )

    for node in lay.nodes.values():
        stroke = rgb(_DANGLING if node.dangling else _OK)
        dash = ' stroke-dasharray="5,4"' if node.dangling else ""
        parts.append(
            f'<rect x="{node.x:.1f}" y="{node.y:.1f}" width="{NODE_W}" height="{NODE_H}" '
            f'rx="4" fill="white" stroke="{stroke}" stroke-width="1.4"{dash}/>'
        )
        parts.append(
            f'<text x="{node.x + NODE_W / 2:.1f}" y="{node.y + 18:.1f}" font-size="11" '
            f'text-anchor="middle" fill="#111">{_esc(node.name)}</text>'
        )
        if node.dangling:
            parts.append(
                f'<text x="{node.x + NODE_W / 2:.1f}" y="{node.y + NODE_H + 11:.1f}" '
                f'font-size="8" text-anchor="middle" fill="{rgb(_DANGLING)}">'
                f'no transform · {_esc(node.orphan_topic)}</text>'
            )

    if findings:
        y = lay.height + 18
        parts.append(f'<text x="{MARGIN}" y="{y:.0f}" font-size="12" font-weight="bold" '
                     f'fill="#111">{len(findings)} finding(s)</text>')
        for f in findings:
            y += 16
            colour = _BAD if f.severity >= 3 else _WARN
            parts.append(
                f'<text x="{MARGIN}" y="{y:.0f}" font-size="10" fill="{rgb(colour)}">'
                f'{_esc(f.severity.name)}  {_esc(f.summary)}</text>'
            )
    parts.append("</svg>")
    return "\n".join(parts)


# ----------------------------------------------------------------------------- pdf


def _pdf_escape(s: str) -> str:
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def to_pdf(report: TransformReport, findings: list[Finding] | None = None) -> bytes:
    """A single-page PDF, written by hand.

    No graphviz and no reportlab: PDF's imaging model is a handful of operators, and the
    base-14 fonts need no embedding, so the whole thing is a few hundred bytes of content
    stream. That keeps `baglens frames --out x.pdf` working on a field laptop with
    nothing installed, which is the machine that most needs it.
    """
    lay = layout(report)
    findings = findings or []
    panel = 30.0 + 14.0 * (len(findings) + 1) if findings else 0.0
    width, height = lay.width, lay.height + panel
    ops: list[str] = []

    def y(v: float) -> float:  # PDF's origin is bottom-left; the layout's is top-left
        return height - v

    def col(c: tuple[float, float, float], stroke: bool = True) -> str:
        return f"{c[0]:.3f} {c[1]:.3f} {c[2]:.3f} {'RG' if stroke else 'rg'}"

    ops.append(f"1 1 1 rg 0 0 {width:.1f} {height:.1f} re f")

    for parent, child, colour, label, dashed in lay.edges:
        a, b = lay.nodes[parent], lay.nodes[child]
        x1, y1 = a.x + NODE_W, a.y + NODE_H / 2
        x2, y2 = b.x, b.y + NODE_H / 2
        mx = (x1 + x2) / 2
        ops.append(col(colour))
        ops.append("1.6 w")
        ops.append("[5 4] 0 d" if dashed else "[] 0 d")
        ops.append(f"{x1:.1f} {y(y1):.1f} m {mx:.1f} {y(y1):.1f} {mx:.1f} {y(y2):.1f} "
                   f"{x2:.1f} {y(y2):.1f} c S")
        # arrow head
        ops.append(col(colour, stroke=False))
        ops.append(f"{x2:.1f} {y(y2):.1f} m {x2 - 7:.1f} {y(y2 - 3.5):.1f} l "
                   f"{x2 - 7:.1f} {y(y2 + 3.5):.1f} l f")
        if label:
            ops.append("BT /F1 8 Tf " + col(colour, stroke=False))
            ops.append(f"1 0 0 1 {mx - len(label) * 2.0:.1f} {y((y1 + y2) / 2 - 5):.1f} Tm "
                       f"({_pdf_escape(label)}) Tj ET")

    for node in lay.nodes.values():
        colour = _DANGLING if node.dangling else _OK
        ops.append(col(colour))
        ops.append("1.4 w")
        ops.append("[5 4] 0 d" if node.dangling else "[] 0 d")
        ops.append(f"{node.x:.1f} {y(node.y + NODE_H):.1f} {NODE_W} {NODE_H} re S")
        ops.append("0.07 0.07 0.07 rg")
        label = node.name
        ops.append("BT /F1 10 Tf "
                   f"1 0 0 1 {node.x + NODE_W / 2 - len(label) * 2.6:.1f} "
                   f"{y(node.y + 18):.1f} Tm ({_pdf_escape(label)}) Tj ET")
        if node.dangling:
            sub = f"no transform - {node.orphan_topic}"
            ops.append(col(_DANGLING, stroke=False))
            ops.append("BT /F1 7 Tf "
                       f"1 0 0 1 {node.x + NODE_W / 2 - len(sub) * 1.7:.1f} "
                       f"{y(node.y + NODE_H + 10):.1f} Tm ({_pdf_escape(sub)}) Tj ET")

    if findings:
        ypos = lay.height + 16
        ops.append("0.07 0.07 0.07 rg")
        ops.append(f"BT /F2 11 Tf 1 0 0 1 {MARGIN} {y(ypos):.1f} Tm "
                   f"({len(findings)} finding\\(s\\)) Tj ET")
        for f in findings:
            ypos += 14
            ops.append(col(_BAD if f.severity >= 3 else _WARN, stroke=False))
            line = f"{f.severity.name}  {f.summary}"[:150]
            ops.append(f"BT /F1 9 Tf 1 0 0 1 {MARGIN} {y(ypos):.1f} Tm "
                       f"({_pdf_escape(line)}) Tj ET")

    content = "\n".join(ops).encode("latin-1", "replace")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width:.1f} {height:.1f}] "
         f"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>").encode(),
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        # WinAnsiEncoding, not the default: StandardEncoding maps 0x27 to a typographic
        # right-quote, so every apostrophe in a finding came out as a curly one
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
        b"/Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    return bytes(out)


def render(report: TransformReport, findings: list[Finding], path: str | Path) -> Path:
    """Write the tree to `path`; the format is chosen by its suffix."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        p.write_bytes(to_pdf(report, findings))
    elif suffix == ".svg":
        p.write_text(to_svg(report, findings))
    elif suffix in (".dot", ".gv"):
        p.write_text(to_dot(report, findings))
    else:
        raise ValueError(f"unsupported frames output {p.suffix!r}; use .pdf, .svg or .dot")
    return p


# ----------------------------------------------------------------------------- cli


def render_text(report: TransformReport, findings: list[Finding]) -> str:
    """The tree and its diagnosis as text — what an agent reads, and what fits in a
    terminal on a field laptop."""
    lines: list[str] = []
    if findings:
        lines.append(f"{len(findings)} transform finding(s):")
        for f in findings:
            lines.append(f"  {f.severity.name:<8} {f.summary}")
            if f.interpretation:
                lines.append(f"           {f.interpretation}")
        lines.append("")
    else:
        lines.append("No transform problems found.")
        lines.append("")

    lines.append(f"tree: {len(report.edges)} transform(s), roots {report.roots or '—'}")
    for e in report.edges:
        _colour, label = _edge_colour(e)
        lines.append(f"  {e.parent:>22} -> {e.child:<24} {label}")
    if report.orphan_frames:
        lines.append("")
        lines.append("frames nothing provides:")
        for frame, topic in report.orphan_frames.items():
            lines.append(f"  {frame:<24} used by {topic}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    from .detectors.auditor import Auditor
    from .readers.base import open_bag

    ap = argparse.ArgumentParser(
        prog="baglens frames",
        description="The transform tree, with the diagnosis already applied. "
                    "A view_frames that tells you what is wrong.",
    )
    ap.add_argument("recording", help="a recording to read /tf and /tf_static from")
    ap.add_argument("--out", metavar="FILE",
                    help="write the tree to a .pdf, .svg or .dot file")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable tree and findings, for an agent")
    args = ap.parse_args(argv)

    report = Auditor(open_bag(args.recording),
                     detectors=["cadence", "transforms"]).run()
    tf = report.transforms
    findings = [f for f in report.findings if f.detector == "transforms"]
    if tf is None:
        print("no transform data in this recording")
        return 1

    if args.out:
        path = render(tf, findings, args.out)
        print(f"wrote {path}")
    if args.json:
        print(json.dumps(
            {"transforms": tf.model_dump(mode="json"),
             "findings": [f.model_dump(mode="json") for f in findings]},
            indent=2,
        ))
    elif not args.out:
        print(render_text(tf, findings))
    return 1 if findings else 0
