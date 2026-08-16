# How it works

The detail the README deliberately leaves out. Read it when you want to argue with a
number rather than accept one.

- [The eight detectors](#the-eight-detectors)
- [The health score, in the open](#the-health-score-in-the-open)
- [When it refuses to answer](#when-it-refuses-to-answer)
- [The streaming constraint](#the-streaming-constraint)
- [Performance, measured](#performance-measured)
- [The measurements that went the wrong way](#the-measurements-that-went-the-wrong-way)
- [Why this exists](#why-this-exists)

## The eight detectors

Scored against 200 synthetic recordings with injected, labelled faults — 160 faulted and
40 clean controls:

| Detector | What it catches | Precision | Recall | FP per clean bag |
|---|---|---|---|---|
| `gap` | A topic goes silent | 1.000 | 1.000 | 0.000 |
| `rate_degradation` | A topic slowly *slows down* — the precursor nothing else looks for | 1.000 | 1.000 | 0.000 |
| `jitter` | Timing variance widens before rates change | 1.000 | 1.000 | 0.000 |
| `dropped` | Messages missing, clustered or diffuse | 1.000 | 1.000 | 0.000 |
| `clock_lag` | The recorder falling behind the publishers | 1.000 | 1.000 | 0.000 |
| `clock_step` | NTP corrections and backward time | 1.000 | 1.000 | 0.000 |
| `correlation` | Sensor failure vs. system-wide stall | 1.000 | 1.000 | 0.000 |
| `file_integrity` | Truncated, unindexed and in-progress files | 1.000 | 1.000 | 0.000 |

Reproduce: `uv run python -m evals.integrity.run --regenerate`.

**Read that table with scepticism.** These are synthetic faults from a generator in this
repository, on a synthetic background. Perfect scores mean the detectors and the generator
agree about what a fault looks like — a regression gate and a floor, not evidence of field
accuracy. The field numbers are in [`INJECTED.md`](../evals/integrity/INJECTED.md)
(0.824 recall on real recordings) and
[`REAL_DATA.md`](../evals/integrity/REAL_DATA.md) (0.993 / 0.955 against PX4's own
dropout records across 105 flights).

The one PX4 miss is not a detection failure but a *bounded-state* one: the detector keeps
at most 1000 silent intervals, and on the busiest flight in the corpus one real stall is
evicted. Streaming with fixed memory is the constraint the library is built on, so that
trade is stated rather than quietly relaxed.

## The health score, in the open

An opaque score gets ignored; a legible one gets argued about, and arguments are
engagement.

```
topic_score = 100 · (1 − 0.30·gap_penalty − 0.35·drop_rate
                         − 0.20·jitter_excess − 0.15·degradation)

overall     = (0.5·min(scores) + 0.3·mean(scores) + 0.2·file_score)
              · (1 − 2.5·stalled_fraction)

              where `scores` covers only topics we can actually assess

≥85 trustworthy   ·   60–85 usable with caveats   ·   <60 compromised
```

Weighting the *minimum* heavily is deliberate: one broken critical topic compromises an
investigation regardless of how healthy the other forty are. `gap_penalty` is measured
against 5% of the recording's length, so five missing seconds matter in a two-minute run
and not in an eight-hour one.

Two terms exist because of what real flights did to the first version of this formula:

- **`stalled_fraction`** is the share of the recording lost to system-wide stalls, charged
  **once**, here. `gap_penalty` deliberately ignores that silence, because charging every
  topic for the same stall punished a 115-topic vehicle 115 times for one event.
- **Topics we cannot assess are excluded, not scored.** A topic that never completed
  warmup, or whose modal rate exceeds the rate it ever sustained by 5×, has no rate model
  to be measured against. They are listed with `hz_source: "aperiodic"` and a reason —
  letting a 5-message event topic set a `min()` weighted at 0.5 was, on its own, most of
  why every real recording once read as `compromised`.

Every constant lives in `config.py` and is overridable.

## When it refuses to answer

`unassessable` overrides the score entirely. It is not a worse grade than `compromised`;
it is the tool declining to grade. Four floors, each reported when it is the one that
failed:

| Floor | Default | What it means |
|---|---|---|
| Topics with a measurable rate | 25% | Most of the recording was never actually checked |
| Share of messages on those topics | 50% | The checks that ran saw a minority of the traffic |
| Coverage of the wall clock | 50% | The rest is silence indistinguishable from an idle robot |
| Duration | 20 s | Too short for cadence warmup; every threshold below would be a guess |

This exists because of one recording. An autonomous shuttle bus, parked for its entire
1,492-second run, 70 of whose 110 topics are event-driven, was published as `compromised`
at score **0.0** — a confident, precise, completely wrong judgement about a file where
**0 of 70 topics have a measurable publication rate**.

Fixing the detector that produced the false alarm was necessary and, on its own, made
things worse: the same file then read *trustworthy at 98.7*, because a recording nothing
can be measured in produces few findings, and few findings look exactly like a clean
recording. Both fixes were needed, and neither was correct alone.

## The streaming constraint

Every detector is an **online algorithm with bounded state**. No detector buffers the
recording, needs the end time, or makes a second pass:

- statistics come from Welford and EWMA, never `numpy.mean` over an accumulated array;
- thresholds adapt from a warmup window, never from global file statistics;
- gap lists, lag curves and density timelines are fixed-size, with truncation reported
  rather than hidden.

That costs perhaps 20% more effort and buys two things: 50 GB files audit without being
loaded, and the same code runs unchanged against a live subscription — verified, with
byte-identical verdicts and findings across 34 mid-stream snapshots on three real flights.

**The per-topic bound is only a bound under `BAGLENS_EDGE_PROFILE=1`, and that is gated in
CI.** The default profile keeps up to 1000 gaps per topic on purpose, so one gappy topic
can hold 48 KB — a workstation trade, not a device budget. Measured on a real 118-topic
PX4 flight: 7,360 B on the worst topic by default, **2,016 B under the edge profile**,
which is where the "<2 KB per topic" claim lives.

## Performance, measured

On real recordings, not synthetic ones (WSL2, single thread):

| Metric | 66 MB PX4 `.ulg`, 118 topics | 886 MB ROS 2 MCAP, 4 topics |
|---|---|---|
| Arrival scan (payload-free) | 205,000 msg/s · 16 MB/s | 21,000 msg/s · 766 MB/s |
| Full audit, all 8 detectors | 44,500 msg/s · 3.5 MB/s | 13,800 msg/s · 501 MB/s |
| Peak RSS | 257 MB | 39 MB |
| State per topic (edge profile) | 2,016 B | 1,984 B |

Two caveats a reader should have rather than a round number:

- **The 257 MB is the reader, not the detectors.** `pyulog` parses an entire ULog into
  numpy before the first arrival is seen. The detectors hold under 1 MB of it, and the
  streaming formats stay at 39 MB regardless of file size. The bounded-state claim is
  about the detectors and it holds; the ULog *reader* is not streaming, and that is the
  last place this project violates its own constraint.
- **A live snapshot costs more than the audit.** Snapshotting at 1 Hz on a 2.7 kHz stream
  roughly doubles the run — the previously published "~14%" was never measured and was
  wrong. One snapshot is ~80 ms on a 311 s flight, down from 162 ms, and it is `finish()`,
  not serialisation, that dominates. Measure your own cadence with
  `scripts/bench_snapshot.py`.

MB/s is dominated by *message count*, not bytes: the audit never parses payloads, so a bag
full of camera frames audits far faster per megabyte than tiny synthetic messages do.

## The measurements that went the wrong way

This section is the reason to trust the others.

**0.832 → 0.381 → 0.955.** An early precision figure of 0.832 was measured across twelve
flights — the twelve that `audit_corpus.py` had ranked *most interesting*, which is a
selection effect. Running the identical eval over all 105 moved it to 0.381 with recall
unchanged. The detector did not get worse; the measurement got honest. It is now 0.955,
after two real defects were found and fixed. **Any number measured on a subset that
something selected should be assumed flattering until re-run on everything.**

**239 unmatched findings → 7.** `correlation` makes two different claims —
`system-wide stall` ("the recorder stopped") and `subsystem failure` ("a shared driver
died") — and the eval scored both against dropout labels, which are evidence for the first
and silent about the second. Splitting them
([`FP_SPLIT.md`](../evals/integrity/FP_SPLIT.md)) exposed the real defect: a topic counted
as "co-silent" whenever it merely hadn't published recently. A 1 Hz topic is idle across
any 0.4 s window by construction, so short gaps collected a dozen innocent bystanders
each.

**A rejected fix that was rejected for the wrong reason.** Making `correlation` honour
`unassessable` looked like the obvious fix for the parked-shuttle-bus artefact. It was
tried four ways and appeared to cost 22+ points of recall against real labels every time,
so it was refused and the refusal was written down in detail. It was wrong: those runs
happened while a *separate* bug — an interval cap that ranked by duration — was
independently evicting 35 of 152 labelled dropouts. Re-measured after that bug was fixed,
the restriction costs no recall at all and gains precision
([`W15_RULES.md`](../evals/integrity/W15_RULES.md)).

> **A measurement taken while another bug is live can convict the wrong change.** When a
> fix is rejected on evidence, re-run that evidence after the next bug is found.

**A number with no script behind it is a guess.** The two figures that turned out wrong —
"<2 KB per topic" and "~14% snapshot overhead" — were the two with no harness regenerating
them. Every published claim now has one: `bench.py`, `bench_snapshot.py`,
`split_false_positives.py`, `ros2_data.py`, `w15_rules.py`.

## Why this exists

| Project | What it does | Where it stops |
|---|---|---|
| `binabik-ai/mcp-rosbags` | Reads `.bag`, `.db3`, `.mcap`; message querying, trajectories, TF | One bag at a time; no persistent index; no cross-mission reasoning |
| `rosbags-mcp` + MCP Lab UI | Pulls data from bags, plots with Plotly | Per-file; plotting-oriented rather than investigation-oriented |
| ROSBag MCP Server (arXiv 2511.03497) | Academic; trajectories, laser scans, transforms | Research release; mobile-robotics scoped; single-bag |
| `ros-mcp-server` / RobotMCP | **Live** robot control — publish to topics, call services | Not a log-analysis tool; the opposite direction |
| Foxglove | Best-in-class visualisation and data management | Human-in-the-loop GUI, not an agent tool surface |
| **baglens** | Integrity auditing, a fleet catalog, cross-mission comparison, provenance on every claim | Not a visualiser, not a robot controller, not a cloud platform |

The gap: **nothing tells you your data is wrong.** `rosbag2` drops messages under load —
users report frame loss well below disk throughput limits — and you find out three weeks
later, mid-investigation. Foxglove will plot a recorder stall as if it were sensor data,
beautifully.
