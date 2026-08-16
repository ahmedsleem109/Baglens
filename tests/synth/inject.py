"""Inject **labelled** faults into **real** recordings.

`generate.py` writes synthetic faults onto a synthetic background, which can only prove
that the generator and the detectors agree about what a fault looks like. This module
applies the same fault shapes to a real `ros2 bag record` MCAP: real jitter, real
burstiness, real topic mix, real QoS metadata, real payloads — plus one fault whose
location is known exactly, because we put it there.

What that buys, precisely: it proves an *injected* fault is caught against a real
background. It does not prove every naturally-occurring fault is caught. That is still
one rung above a synthetic corpus and one rung below an instrumented robot.

The output is byte-for-byte a normal MCAP with a `<name>.ground_truth.json` sidecar in
exactly the schema `tests/synth/generate.py` emits, so `evals/integrity/run.py`'s scorer
reads both corpora without knowing which is which.

    uv run python -m tests.synth.inject --sources ~/data/public/ros2 --out /tmp/baglens-injected

Memory discipline: the source files run to gigabytes, so nothing is buffered. Messages
stream through a bounded reorder heap (a clock step or a jitter kick can move a message
past its neighbour, and MCAP wants log-time order), and copies are sliced to a window so
the corpus fits on a laptop disk.
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import random
import shutil
from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcap.reader import make_reader
from mcap.writer import CompressionType, Writer

from baglens.config import CONFIG
from baglens.readers.stamp_peek import peek_stamp_ns, stamp_offset, write_stamp_ns

from .generate import (
    Fault,
    clock_step,
    correlated_stall,
    diffuse_drops,
    jitter_injection,
    rate_degradation,
    recorder_lag,
    topic_dropout,
    truncation,
)

#: how far out of order a message may be pushed before the writer would see it late.
#: A backward clock step of 2 s on a 2 kHz topic is ~4000 messages; 20k is comfortable
#: and costs a few MB.
REORDER_BUFFER = 20_000


# --------------------------------------------------------------------------- probing


def summarise(path: Path) -> dict[str, Any]:
    """Topic inventory, rates and time span, read from the MCAP summary alone.

    The summary is a few kilobytes at the end of the file, so this is O(1) in file
    size — it does not scan the 1.3 GB of messages to tell you the recording is 1492 s
    long.
    """
    with path.open("rb") as f:
        summary = make_reader(f).get_summary()
    if summary is None or summary.statistics is None:
        raise ValueError(f"{path} has no summary; cannot plan an injection against it")
    st = summary.statistics
    topic_of = {c.id: c.topic for c in summary.channels.values()}
    duration = (st.message_end_time - st.message_start_time) / 1e9
    # Every declared channel, not only the ones with messages: `channel_message_counts`
    # omits an empty channel entirely, and a copy whose fault emptied a topic would then
    # look like a copy that never had it — which is a different recording, not a faulted
    # one. The correlation denominator reads the declared list.
    topics = [
        {
            "topic": topic_of.get(cid, str(cid)),
            "count": st.channel_message_counts.get(cid, 0),
            "hz": st.channel_message_counts.get(cid, 0) / duration if duration > 0 else 0.0,
        }
        for cid in topic_of
    ]
    topics.sort(key=lambda t: -t["count"])
    return {
        "path": path,
        "start_time_ns": st.message_start_time,
        "end_time_ns": st.message_end_time,
        "duration_s": duration,
        "message_count": st.message_count,
        "n_channels": len(summary.channels),
        "bytes": path.stat().st_size,
        "topics": topics,
    }


def profile_window(path: Path, t0_ns: int, t1_ns: int) -> dict[str, Any]:
    """Per-topic counts and rates **inside the window that will actually be copied**.

    The summary describes the whole file, and on a long recording that is a different
    robot. `nuway_stops` averages 5.3 Hz on `/bond` over 1492 s; across the first 131 s
    that topic does not publish at all, so a dropout planned from the summary removed
    zero messages and produced a label with nothing behind it. One extra pass over the
    window is cheap next to shipping a corpus of empty labels.
    """
    counts: dict[str, int] = {}
    first: dict[str, int] = {}
    last: dict[str, int] = {}
    #: one-second occupancy per topic. A mean rate cannot tell a metronome from a burst,
    #: and a fault window placed on a burst topic's quiet stretch removes nothing —
    #: `/bond` averages 5.3 Hz on the shuttle bus and is absent for tens of seconds at a
    #: time. Occupancy is what says whether this topic can carry a label at all.
    live: dict[str, set[int]] = {}
    lo = hi = None
    for _schema, chan, msg in _stream(path, t0_ns, t1_ns):
        tp = chan.topic
        counts[tp] = counts.get(tp, 0) + 1
        first.setdefault(tp, msg.log_time)
        last[tp] = msg.log_time
        if lo is None:
            lo = msg.log_time
        hi = msg.log_time
        live.setdefault(tp, set()).add(int((msg.log_time - t0_ns) / 1e9))
    duration = ((hi or 0) - (lo or 0)) / 1e9
    n_buckets = max(int(duration) + 1, 1)
    topics = [
        {
            "topic": tp,
            "count": n,
            # rate over the span this topic was alive for, not over the window: a topic
            # that starts halfway through is not publishing at half its rate
            "hz": n / max((last[tp] - first[tp]) / 1e9, 1e-9) if n > 1 else 0.0,
            "coverage": len(live[tp]) / n_buckets,
        }
        for tp, n in counts.items()
    ]
    topics.sort(key=lambda t: -t["count"])
    return {"duration_s": duration, "topics": topics, "message_count": sum(counts.values()),
            "live": live}


def periodic_topics(info: dict[str, Any], min_hz: float = 1.0,
                    min_coverage: float = 0.05) -> list[str]:
    """Topics that can carry a label: fast enough, and publishing throughout.

    A fault injected into a 0.2 Hz topic is not a fault anyone could detect — the gap it
    makes is shorter than the topic's own period. And a fault injected into a bursty
    topic's quiet stretch removes nothing at all, which is worse: it produces a label
    with no fault behind it, and every detector scores a miss on something that was
    never there. Both filters are here rather than in the scorer because a label that
    cannot be honest should not be written in the first place.

    The coverage floor is low on purpose: `place` is what guarantees the fault window
    contains live messages, and this only rejects topics that barely appear at all. The
    shuttle-bus recording opens with 113 seconds in which *nothing* publishes, so all 70
    of its topics sit at 0.14 coverage, and a stricter floor would refuse to label the
    one recording this project most needs labelled.
    """
    return [
        t["topic"] for t in info["topics"]
        if t["hz"] >= min_hz and t.get("coverage", 1.0) >= min_coverage
    ]


def place(window: float, duration: float, live: dict[str, set[int]], topics: tuple[str, ...],
          prefer: float) -> float | None:
    """The earliest start at or after `prefer` where every named topic is live throughout.

    Returns None when there is no such placement, which is the honest answer for a
    recording whose topics never overlap for that long.
    """
    need = max(int(window), 1)
    latest = int(duration) - need
    if latest < 0:
        return None
    for start in list(range(int(prefer), latest + 1)) + list(range(0, int(prefer))):
        if all(
            all(b in live.get(tp, set()) for b in range(start, start + need))
            for tp in topics
        ):
            return float(start)
    return None


# ------------------------------------------------------------------- fault semantics

Rule = Callable[[str, float, random.Random], bool]
Shift = Callable[[str, float, random.Random], float]


def _drop_rules(faults: list[Fault], duration: float) -> list[tuple[int, Rule]]:
    """[(fault index, rule)]; the rule answers "is this message removed?".

    The index travels with the rule so `inject` can count what each fault actually did.
    A fault that removed nothing is not a fault, and a label with nothing behind it is
    worse than no label — it is a free miss for every detector.
    """
    rules: list[tuple[int, Rule]] = []
    for fi, f in enumerate(faults):
        if f.kind in ("topic_dropout", "correlated_stall"):
            targets = set(f.topics) | ({f.topic} if f.topic else set())
            lo, hi = f.t_start, f.t_end

            def rule(tp: str, t: float, _rng: random.Random,
                     targets=targets, lo=lo, hi=hi) -> bool:
                return tp in targets and lo <= t < hi

            rules.append((fi, rule))
        elif f.kind == "diffuse_drops":
            p = f.params["p"]
            lo = f.t_start
            hi = f.t_end if f.t_end > f.t_start else duration

            def rule(tp: str, t: float, rng: random.Random,
                     target=f.topic, p=p, lo=lo, hi=hi) -> bool:
                return tp == target and lo <= t <= hi and rng.random() < p

            rules.append((fi, rule))
        elif f.kind == "rate_degradation":
            # Thinning, not resampling: the real inter-arrival texture is what makes this
            # worth doing, and rebuilding the schedule would throw it away. Dropping a
            # ramped fraction lowers the observed rate by exactly that fraction.
            a, b = f.params["from_hz"], f.params["to_hz"]
            t0, t1 = f.t_start, f.t_end

            def rule(tp: str, t: float, rng: random.Random,
                     target=f.topic, a=a, b=b, t0=t0, t1=t1) -> bool:
                if tp != target or t < t0:
                    return False
                frac = 1.0 if t >= t1 else (t - t0) / max(t1 - t0, 1e-9)
                keep = (a + (b - a) * frac) / max(a, 1e-9)
                return rng.random() > keep

            rules.append((fi, rule))
    return rules


def _shifts(faults: list[Fault]) -> list[tuple[int, Shift]]:
    """[(fault index, shift)]; the shift answers "how much later is this message?"."""
    out: list[tuple[int, Shift]] = []
    for fi, f in enumerate(faults):
        if f.kind == "clock_step":
            delta = f.params["step_ms"] / 1000.0
            if f.params.get("direction") == "backward":
                delta = -delta
            out.append((fi, lambda tp, t, _r, at=f.t_start, d=delta: d if t >= at else 0.0))
        elif f.kind == "recorder_lag":
            growth = f.params["growth_ms_per_min"] / 1000.0 / 60.0

            def lag(tp: str, t: float, _r: random.Random, g=growth, t0=f.t_start) -> float:
                return g * (t - t0) if t > t0 else 0.0

            out.append((fi, lag))
        elif f.kind == "jitter_injection":
            # Displace each arrival by a lognormal multiple of the topic's own period,
            # which is what a scheduler under load does. The displacement is centred so
            # the *rate* is unchanged and only the variance moves — otherwise this fault
            # would be indistinguishable from rate_degradation.
            cv = f.params["cv_target"]
            sigma = math.sqrt(math.log(1.0 + cv * cv))

            def kick(tp: str, t: float, rng: random.Random, target=f.topic,
                     lo=f.t_start, hi=f.t_end, sigma=sigma,
                     period=f.params.get("period_s", 0.02)) -> float:
                if tp != target or not (lo <= t <= hi):
                    return 0.0
                return period * (math.exp(rng.gauss(0.0, sigma) - sigma * sigma / 2) - 1.0)

            out.append((fi, kick))
    return out


def _stamp_ramps(faults: list[Fault]) -> list[tuple[int, Callable[[str, float], float]]]:
    """[(fault index, extra age in seconds)] for stale-pipeline faults (F1).

    This is the only injector that edits a payload, and it edits exactly eight bytes of
    it: the stamp is moved *earlier*, so the data is older when it is published. Arrival
    times, message sizes and the topic mix are untouched, which is what makes the result
    attributable — the detector has nothing else to react to.
    """
    out: list[tuple[int, Callable[[str, float], float]]] = []
    for fi, f in enumerate(faults):
        if f.kind != "stale_pipeline" or not f.topic:
            continue

        def ramp(tp: str, t: float, target=f.topic, a=f.params["from_ms"] / 1000.0,
                 b=f.params["to_ms"] / 1000.0, t0=f.t_start, t1=f.t_end) -> float:
            if tp != target:
                return 0.0
            if t <= t0:
                return a
            if t >= t1:
                return b
            return a + (b - a) * (t - t0) / max(t1 - t0, 1e-9)

        out.append((fi, ramp))
    return out


# ---------------------------------------------------------------------------- writing


def _stream(src: Path, t0_ns: int, t1_ns: int) -> Iterator[tuple[Any, Any, Any]]:
    with src.open("rb") as f:
        reader = make_reader(f)
        yield from reader.iter_messages(start_time=t0_ns, end_time=t1_ns, log_time_order=True)


def inject(
    source: str | Path,
    dest: str | Path,
    faults: list[Fault],
    *,
    seed: int = 0,
    window_s: float | None = None,
    start_offset_s: float = 0.0,
    compression: bool = True,
) -> dict[str, Any]:
    """Write a corrupted copy of ``source`` with ``faults`` applied. Returns ground truth.

    Fault times are relative to the first message of the *copy*, which is what the
    auditor reports against, so labels and findings share an origin without either side
    having to know the source file's epoch.
    """
    source, dest = Path(source), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    info = summarise(source)
    t0_ns = info["start_time_ns"] + int(start_offset_s * 1e9)
    # `iter_messages` treats `end_time` as exclusive, so copying a whole file has to ask
    # for one nanosecond past its last message. Without the +1 every copy silently lost
    # its final message — invisible in a 200,000-message recording, and exactly the kind
    # of quiet discrepancy that makes a corpus untrustworthy later.
    full_end = info["end_time_ns"] + 1
    t1_ns = full_end if window_s is None else t0_ns + int(window_s * 1e9)
    t1_ns = min(t1_ns, full_end)
    duration = (t1_ns - t0_ns) / 1e9

    drops = _drop_rules(faults, duration)
    shifts = _shifts(faults)
    stamp_ramps = _stamp_ramps(faults)

    with source.open("rb") as sf:
        summary = make_reader(sf).get_summary()
    assert summary is not None

    kept: dict[str, int] = {}
    #: fault index -> messages it removed or moved. Zero means the fault was planned
    #: against a topic that does not publish in this window, and the label is void.
    affected: dict[int, int] = {}
    first_ns: int | None = None
    last_ns = 0

    with dest.open("wb") as out:
        writer = Writer(
            out,
            compression=CompressionType.ZSTD if compression else CompressionType.NONE,
            chunk_size=1 << 20,
        )
        writer.start(profile="ros2", library="baglens-inject")

        # Every channel is registered up front, including ones this window empties out.
        # The topic *inventory* is part of what makes the background real — the
        # correlation denominator and the per-topic scores both read it — and a topic
        # that vanishes because its only messages were dropped would quietly change the
        # recording's shape beyond the injected fault.
        schema_map: dict[int, int] = {}
        for schema in summary.schemas.values():
            schema_map[schema.id] = writer.register_schema(
                name=schema.name, encoding=schema.encoding, data=schema.data
            )
        chan_map: dict[int, int] = {}
        topic_of: dict[int, str] = {}
        for chan in summary.channels.values():
            topic_of[chan.id] = chan.topic
            chan_map[chan.id] = writer.register_channel(
                topic=chan.topic,
                message_encoding=chan.message_encoding,
                schema_id=schema_map.get(chan.schema_id, 0),
                metadata=dict(chan.metadata or {}),
            )

        # bounded reorder heap: a shifted message may land behind one already emitted
        heap: list[tuple[int, int, int, int, int, bytes]] = []
        tiebreak = 0

        def flush(limit: int) -> None:
            while len(heap) > limit:
                log_ns, _tb, cid, pub_ns, seq, data = heapq.heappop(heap)
                writer.add_message(channel_id=cid, log_time=log_ns,
                                   publish_time=pub_ns, data=data, sequence=seq)

        stamp_offsets: dict[int, int | None] = {}

        for _schema, chan, msg in _stream(source, t0_ns, t1_ns):
            topic = topic_of.get(chan.id, chan.topic)
            rel = (msg.log_time - t0_ns) / 1e9
            removed = False
            for fi, rule in drops:
                if rule(topic, rel, rng):
                    affected[fi] = affected.get(fi, 0) + 1
                    removed = True
                    break
            if removed:
                continue
            delta = 0.0
            for fi, shift in shifts:
                d = shift(topic, rel, rng)
                if d:
                    affected[fi] = affected.get(fi, 0) + 1
                    delta += d
            log_ns = msg.log_time + int(delta * 1e9)
            if first_ns is None or log_ns < first_ns:
                first_ns = log_ns
            last_ns = max(last_ns, log_ns)
            data = msg.data
            for fi, ramp in stamp_ramps:
                extra = ramp(topic, rel)
                if not extra:
                    continue
                off = stamp_offsets.get(chan.schema_id, False)
                if off is False:
                    sch = summary.schemas.get(chan.schema_id)
                    off = stamp_offset(
                        sch.data.decode("utf-8", "replace"), sch.encoding or "ros2msg"
                    ) if sch is not None else None
                    stamp_offsets[chan.schema_id] = off
                if off is None:
                    continue
                cur = peek_stamp_ns(data, off)
                if cur is None or cur == 0:
                    continue
                data = write_stamp_ns(data, off, cur - int(extra * 1e9))
                affected[fi] = affected.get(fi, 0) + 1

            kept[topic] = kept.get(topic, 0) + 1
            tiebreak += 1
            heapq.heappush(
                heap,
                (log_ns, tiebreak, chan_map[chan.id], msg.publish_time, msg.sequence, data),
            )
            flush(REORDER_BUFFER)
        flush(0)
        writer.finish()

    truth: dict[str, Any] = {
        "path": str(dest),
        "seed": seed,
        "duration_s": (last_ns - first_ns) / 1e9 if first_ns is not None else 0.0,
        "base_epoch_ns": first_ns or 0,
        "source": str(source),
        "source_window": [start_offset_s, start_offset_s + duration],
        "injected": True,
        "topics": [
            {"topic": t["topic"], "hz": t["hz"], "count": kept.get(t["topic"], 0)}
            for t in info["topics"]
        ],
        "faults": [asdict(f) for f in faults],
        "clean": not faults,
    }
    # Truncation and CRC corruption are byte operations with no message to count, so they
    # are effective by construction; everything else has to prove it changed something.
    for i, f in enumerate(truth["faults"]):
        n = affected.get(i, 0)
        f["messages_affected"] = n
        f["effective"] = bool(n) or faults[i].kind in ("truncation", "crc_corruption")

    for f in faults:
        if f.kind == "truncation":
            raw = dest.read_bytes()
            keep = max(1024, int(len(raw) * f.params["fraction"]))
            dest.write_bytes(raw[:keep])
            truth["truncated_to_bytes"] = keep
            truth["original_bytes"] = len(raw)
            # a truncated file's real span is unknown to the reader; the label stays the
            # span that was written, and the scorer treats truncation as a whole-run fault

    dest.with_suffix(".ground_truth.json").write_text(json.dumps(truth, indent=2))
    return truth


# ------------------------------------------------------------------------- the corpus


def plan_for(info: dict[str, Any], duration: float, rng: random.Random
             ) -> list[tuple[str, list[Fault]]]:
    """One variant per fault kind, sized to the recording actually in front of us.

    Windows are a fraction of the copy's duration rather than fixed seconds: a 20 s
    slice of a 2 kHz CAN bus and a 300 s slice of a parked shuttle bus cannot take the
    same 8-second dropout and mean the same thing by it.

    **A label a detector could not satisfy is not a test of the detector.** Three of the
    fault kinds have a documented floor below which the detector does not claim to fire —
    D3 needs `min_buckets * bucket_s` of history, D4 needs a full `jitter.window` of
    inter-arrivals, D6 fires on absolute lag rather than on a slope — and the first run
    of this corpus wrote labels underneath all three, on 16- and 37-second recordings.
    Those labels measured the length of the recording, not the accuracy of the detector.
    Magnitudes here are matched to the synthetic corpus so the two numbers compare, and
    faults whose floor the recording cannot clear are not written at all. Neither the
    detectors nor their thresholds were touched; see `INJECTED.md` for both numbers.
    """
    fast = periodic_topics(info, min_hz=1.0)
    hz_of = {t["topic"]: t["hz"] for t in info["topics"]}
    count_of = {t["topic"]: t["count"] for t in info["topics"]}
    live: dict[str, set[int]] = info.get("live", {})
    d3, d4, d6 = CONFIG.degradation, CONFIG.jitter, CONFIG.clock

    # start faults after a warmup: every detector adapts its threshold from the opening
    # window, and a fault inside that window is being used to define "normal"
    warm = max(10.0, duration * 0.15)
    span = max(duration - warm - 5.0, 5.0)
    hole = min(max(duration * 0.05, 3.0), 15.0)

    out: list[tuple[str, list[Fault]]] = []

    # Whole-run faults need no placement: they apply to whatever the recording contains.
    out.append(("step", [clock_step(warm + span * 0.6, rng.choice([800.0, 1500.0, 2500.0]),
                                    rng.choice(["forward", "backward"]))]))
    # Lag is specified as an end-state, not as a rate: D6 fires on total growth and on
    # absolute lag, so "120 ms per minute" is a 3.7 s fault on the shuttle bus and a 32 ms
    # one — comfortably under its 100 ms floor — on a 16 s slice of the Tesla.
    target_lag_s = max(3.0 * d6.lag_growth_s, 3.0 * d6.lag_absolute_s)
    out.append(("lag", [recorder_lag(target_lag_s * 1000.0 * 60.0 / max(span, 1e-9), warm)]))
    out.append(("truncate", [truncation(0.7)]))

    if not fast:
        # No topic publishes steadily enough to carry a windowed label. That is a real
        # property of the recording — a parked robot's topics are event-driven — and the
        # honest response is three labels instead of eight, not eight optimistic ones.
        return out

    primary = fast[0]
    stall_set = tuple(fast[:4])
    # A second, independent topic so consecutive variants do not all land on one channel —
    # but only one with more messages than D4's variance window, since a topic that never
    # fills that window has no jitter baseline to expand.
    jit_min = 3 * d4.window
    others = [tp for tp in fast[1:] if count_of.get(tp, 0) >= jit_min]
    secondary = others[0] if others else (primary if count_of.get(primary, 0) >= jit_min else "")

    t_drop = place(hole, duration, live, (primary,), warm + span * 0.2)
    if t_drop is not None:
        out.append(("dropout", [topic_dropout(primary, t_drop, hole)]))

    stall_len = min(hole, 8.0)
    t_stall = place(stall_len, duration, live, stall_set, warm + span * 0.5)
    if t_stall is not None:
        out.append(("stall", [correlated_stall(stall_set, t_stall, stall_len)]))

    jit_len = min(span * 0.4, 60.0)
    t_jit = place(jit_len, duration, live, (secondary,), warm + span * 0.3) if secondary else None
    if t_jit is not None and jit_len >= d4.sustain_s:
        jit = jitter_injection(secondary, 0.7, t_jit, jit_len)
        jit.params["period_s"] = 1.0 / max(hz_of.get(secondary, 10.0), 1e-3)
        out.append(("jitter", [jit]))

    out.append(("thin", [diffuse_drops(primary, 0.2)]))

    # D3 reads a Theil-Sen slope over at most `n_buckets` buckets, so a ramp spread across
    # a 1843 s recording is invisible to it however deep the ramp goes: only 300 s of it is
    # ever in view, and 4 Hz of change on a 50 Hz topic is under the 15% slope it fires on.
    # The ramp is therefore sized to the detector's window rather than to the recording,
    # which is also what the synthetic corpus does (a 95 s ramp inside a 120 s bag).
    d3_history = d3.min_buckets * d3.bucket_s
    if span >= d3_history:
        ramp = min(span * 0.8, d3.n_buckets * d3.bucket_s)
        out.append(("degrade", [rate_degradation(primary, hz_of[primary],
                                                 hz_of[primary] * 0.5, warm, ramp)]))
    return out


#: (filename, window seconds or None for the whole file). Windows are chosen so each
#: copy stays around 100 MB — the sources total 5 GB and a full-size corpus would not
#: fit beside them. `bytes_per_second` from the summary is what sets them.
DEFAULT_BASES: tuple[tuple[str, float | None], ...] = (
    ("nuway_waypoints.mcap", None),   # 72 MB, 1843 s, 5 topics — the cheap long one
    ("nuway_stops.mcap", 180.0),      # the W15 recording: 110 topics, mostly event-driven
    ("dongkkka_00.mcap", None),       # 37 s, 11 topics at 100 Hz — short and dense
    ("tesla3_av.mcap", 16.0),         # a 2 kHz CAN bus
    ("fastlivo_hku2.mcap", 16.0),     # lidar + two cameras + a 200 Hz IMU
)


def build_corpus(sources: Path, out: Path, bases=DEFAULT_BASES, seed: int = 20260815,
                 max_gb: float = 6.0, quiet: bool = False) -> list[dict[str, Any]]:
    """Write one clean control plus one variant per fault kind for each base recording."""
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    truths: list[dict[str, Any]] = []
    written = 0

    for i, (name, window) in enumerate(bases):
        src = sources / name
        if not src.exists():
            print(f"skip {name}: not found")
            continue
        info = summarise(src)
        span = info["duration_s"] if window is None else min(window, info["duration_s"])
        per_copy = info["bytes"] * span / max(info["duration_s"], 1e-9)
        stem = src.stem

        # Plan against the window, not the file. The summary's rates are averages over
        # the whole recording, and on a long one they describe a different robot than the
        # slice being copied — see `profile_window`.
        t0_ns = info["start_time_ns"]
        t1_ns = min(t0_ns + int(span * 1e9), info["end_time_ns"])
        win = profile_window(src, t0_ns, t1_ns)
        duration = win["duration_s"]
        if not quiet:
            print(f"{name}: window {duration:.0f}s, {win['message_count']} msgs, "
                  f"{len(win['topics'])} topics, fastest "
                  f"{win['topics'][0]['topic'] if win['topics'] else '-'}")

        variants: list[tuple[str, list[Fault]]] = [("clean", [])]
        variants += plan_for(win, duration, rng)

        if written + per_copy * len(variants) > max_gb * 1e9:
            print(f"skip {name}: {per_copy * len(variants) / 1e9:.1f} GB would exceed "
                  f"the {max_gb:.0f} GB budget")
            continue

        for j, (label, faults) in enumerate(variants):
            dest = out / f"{stem}__{label}.mcap"
            if dest.exists() and dest.with_suffix(".ground_truth.json").exists():
                truths.append(json.loads(dest.with_suffix(".ground_truth.json").read_text()))
                written += dest.stat().st_size
                continue
            truth = inject(src, dest, faults, seed=seed + 100 * i + j, window_s=window)
            truths.append(truth)
            written += dest.stat().st_size
            void = [f["kind"] for f in truth["faults"] if not f["effective"]]
            if not quiet:
                note = f"  VOID LABEL: {', '.join(void)} changed nothing" if void else ""
                print(f"  {dest.name}  {dest.stat().st_size / 1e6:.0f} MB  "
                      f"{truth['duration_s']:.0f}s  "
                      f"{sum(t['count'] for t in truth['topics'])} msgs{note}")
    if not quiet:
        print(f"{len(truths)} copies, {written / 1e9:.2f} GB in {out}")
    return truths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="inject labelled faults into real recordings")
    ap.add_argument("--sources", default="~/data/public/ros2")
    ap.add_argument("--out", default="/tmp/baglens-injected")
    ap.add_argument("--max-gb", type=float, default=6.0)
    ap.add_argument("--seed", type=int, default=20260815)
    args = ap.parse_args(argv)

    sources = Path(args.sources).expanduser()
    out = Path(args.out).expanduser()

    free_gb = shutil.disk_usage(out.parent if out.exists() else Path.home()).free / 1e9
    if free_gb < args.max_gb + 2:
        print(f"refusing: {free_gb:.1f} GB free, budget is {args.max_gb:.1f} GB. "
              "Filling this disk remounts the WSL image read-only.")
        return 1

    build_corpus(sources, out, seed=args.seed, max_gb=args.max_gb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
