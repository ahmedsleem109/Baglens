"""F3 — transform integrity, scored against the four faults the spec names.

The rule is the same as everywhere else: a healthy tree must produce **nothing**, and each
fault must be caught **by name**. A detector that fires on a healthy robot gets muted, and
a muted detector catches nothing at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from baglens.detectors.auditor import Auditor
from baglens.frames import render, render_text, to_dot, to_pdf, to_svg
from baglens.models import Finding, TransformReport
from baglens.readers.base import open_bag
from tests.synth.generate import TF_CONSUMERS, generate_bag, tf_scenario

GATE = ["cadence", "transforms"]


def _bag(tmp: Path, kind: str, seed: int = 3, duration_s: float = 60.0) -> Path:
    path = tmp / f"tf_{kind}.mcap"
    generate_bag(path, seed=seed, duration_s=duration_s, topics=TF_CONSUMERS,
                 tf_edges=tf_scenario(kind))
    return path


def _run(path: Path) -> tuple[TransformReport, list[Finding]]:
    report = Auditor(open_bag(path), detectors=GATE).run()
    assert report.transforms is not None
    return report.transforms, [f for f in report.findings if f.detector == "transforms"]


# ------------------------------------------------------------------ the headline


def test_a_healthy_tree_produces_nothing(tmp_path: Path) -> None:
    _tf, findings = _run(_bag(tmp_path, "healthy"))
    assert findings == [], [f.summary for f in findings]


@pytest.mark.parametrize("seed", range(5))
def test_a_healthy_tree_stays_silent_across_seeds(seed: int, tmp_path: Path) -> None:
    _tf, findings = _run(_bag(tmp_path, "healthy", seed=seed))
    assert findings == [], [f.summary for f in findings]


@pytest.mark.parametrize(
    ("kind", "must_say"),
    [
        ("duplicate_publisher", "more than one source"),
        ("missing_static", "no transform"),
        ("stamped_ahead", "into the future"),
        ("intermittent_chain", "only 78% of the time"),
    ],
)
def test_each_fault_is_caught_and_named(kind: str, must_say: str, tmp_path: Path) -> None:
    _tf, findings = _run(_bag(tmp_path, kind))
    assert findings, f"{kind} produced no finding"
    blob = " ".join(f.summary for f in findings)
    assert must_say in blob, f"{kind}: expected {must_say!r} in {blob!r}"


def test_the_duplicate_publisher_reports_how_far_apart_they_are(tmp_path: Path) -> None:
    """The disagreement is the actionable number — it separates two broadcasters
    fighting from one broadcaster repeating itself."""
    tf, findings = _run(_bag(tmp_path, "duplicate_publisher"))
    dup = next(f for f in findings if "more than one source" in f.summary)
    assert dup.evidence["max_disagreement_m"] == pytest.approx(0.35, abs=0.02)
    edge = next(e for e in tf.edges if (e.parent, e.child) == ("map", "odom"))
    assert edge.disagreements > 100


def test_the_orphan_frame_names_the_topic_that_needs_it(tmp_path: Path) -> None:
    tf, _findings = _run(_bag(tmp_path, "missing_static"))
    assert tf.orphan_frames == {"laser": "/scan"}


# --------------------------------------------------------------------- bounded


def test_state_is_bounded_by_the_tree_not_the_recording(tmp_path: Path) -> None:
    short = Auditor(open_bag(_bag(tmp_path, "healthy", duration_s=30.0)),
                    detectors=GATE)
    short.run()
    long = Auditor(open_bag(_bag(tmp_path, "healthy", seed=9, duration_s=120.0)),
                   detectors=GATE)
    long.run()
    assert long.transforms.state_bytes() == short.transforms.state_bytes()


# --------------------------------------------------------------------- rendering


def test_every_output_format_is_written_and_parses(tmp_path: Path) -> None:
    tf, findings = _run(_bag(tmp_path, "missing_static"))

    svg = to_svg(tf, findings)
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert "laser" in svg and "no transform" in svg

    dot = to_dot(tf, findings)
    assert dot.startswith("digraph tf {") and dot.rstrip().endswith("}")
    assert '"map" -> "odom"' in dot

    pdf = to_pdf(tf, findings)
    assert pdf.startswith(b"%PDF-") and b"startxref" in pdf and pdf.rstrip().endswith(b"%%EOF")


def test_the_pdf_is_readable_by_a_pdf_library(tmp_path: Path) -> None:
    """The point of writing the PDF by hand is that it works with nothing installed —
    so it had better be a real PDF, not merely bytes that start with %PDF."""
    pypdf = pytest.importorskip("pypdf")
    tf, findings = _run(_bag(tmp_path, "missing_static"))
    out = render(tf, findings, tmp_path / "tree.pdf")
    reader = pypdf.PdfReader(str(out))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    for expected in ("map", "odom", "base_link", "laser", "no transform"):
        assert expected in text, f"{expected!r} missing from the rendered page"
    # the diagnosis is on the page, which is the whole difference from view_frames
    assert "HIGH" in text


def test_render_rejects_a_format_it_cannot_write(tmp_path: Path) -> None:
    tf, findings = _run(_bag(tmp_path, "healthy"))
    with pytest.raises(ValueError, match="unsupported"):
        render(tf, findings, tmp_path / "tree.png")


def test_text_output_leads_with_the_diagnosis(tmp_path: Path) -> None:
    tf, findings = _run(_bag(tmp_path, "duplicate_publisher"))
    text = render_text(tf, findings)
    assert text.splitlines()[0].endswith("finding(s):")
    assert "map" in text and "odom" in text


def test_a_healthy_tree_still_renders(tmp_path: Path) -> None:
    """No findings is not no output — the tree is worth seeing either way."""
    tf, findings = _run(_bag(tmp_path, "healthy"))
    assert "No transform problems found." in render_text(tf, findings)
    assert to_svg(tf, findings).startswith("<svg")
