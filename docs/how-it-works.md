# How it works

The detail the README deliberately leaves out. Read it when you want to argue with a
number rather than accept one.

- [The eight detectors](#the-eight-detectors)
- [Data age](#data-age-how-old-was-the-data-behind-that-command)
- [The pre-flight gate](#the-pre-flight-gate-refuse-to-start-a-mission-that-is-already-broken)
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

## Data age: how old was the data behind that command?

The eight detectors above all answer *"can this recording be trusted?"*. This one answers
a different question, and it is the one teams actually ask: **how old was the camera frame
that produced this steering command?** A pipeline that drifts from 80 ms to 300 ms makes a
robot quietly worse, the behaviour looks like a tuning problem, and nothing existing will
tell you which stage grew.

`header.stamp` is the *capture* time, and nodes propagate it as they pass derived results
along. Following it gives the true age of the data behind every topic, per stage:

```
/camera/image_raw   stamp=t          published t+12ms   →  capture→publish  12 ms
/detections         stamp=t          published t+94ms   →  perception       82 ms
/cmd_vel_stamped    (from stamp t)   published t+131ms  →  planning         37 ms
                                                           end-to-end      131 ms
```

**The propagation graph is inferred, not declared.** Nothing tells you that `/detections`
derives from `/camera`. Stamp equality does: a `/detections` message carrying a stamp an
earlier `/camera` message published is a causal link, and coincidences are rare enough
that the edge only has to clear a support threshold to be believed.

**But read the next section before believing the diagram above applies to your robot.**

### What stamp propagation actually looks like on real robots

The worked example above is what F1 was designed for. It is *not* what the public corpus
contains, and that is worth stating before anyone plans around it. Measured across all 11
real ROS 2 recordings — 1% to 21% of stamps are carried by more than one topic:

| Recording | Shared | What is actually sharing |
|---|---:|---|
| dongkkka ×6 | 10.3% | `/zed/left` + `/zed/right` |
| fastlivo | 4.6% | `/left_camera` + `/right_camera` + `/livox/lidar` |
| nuway_stops | 10.3% | `/lidar_safety/*/cloud` + `/sick_lms_1xx/scan` |
| nuway_waypoints | 21.4% | `/imu/data` + `/odometry/global` |
| tesla3_av | 1.0% | `/scan` + `/velodyne_packets` + `/velodyne_points` |

Almost all of it is **sensor synchronisation, not a processing pipeline** — stereo pairs
and hardware-synced sensors publishing one capture instant. The delay between `/zed/left`
and `/zed/right` is ~0 and is not a latency hop. The remainder is driver-internal
derivation (`velodyne_packets → points → scan`). Exactly one edge in the corpus is a
genuine cross-node derivation: `/imu/data → /odometry/global`.

**No recording contains a perception → planning → actuation chain.** `nuway_stops` is a
full Nav2 shuttle bus — 110 topics, costmaps, `/cmd_vel` — and its deepest stamp chain
still stops at the lidar driver. So on this corpus the question *"how old was the camera
frame behind this steering command?"* is **unanswerable**, because real stacks restamp
before the command is published.

Two consequences, both deliberate:

1. The per-stage chain is real machinery and is verified against fixtures that propagate
   stamps end to end — but on a robot that restamps, it degrades to per-topic age. Do not
   plan a latency budget around it without checking your own stack first.
2. The restamp finding stops being a footnote and becomes the point: it **names the node
   that broke the trace**, which is the precondition for anyone measuring latency at all.

The fixtures propagate stamps because they were written from the design sketch rather
than from the corpus. That is the "validated against your own assumptions" trap, and it
is recorded here because it was walked into during F1 and only caught by measurement.

**The distribution, not the mean.** P50/P95/P99 per stage, plus a Theil-Sen trend over
bucketed P99s. The tail moves first — a P99 that doubles across a mission is a finding
while the mean is still flat.

### Reading a payload without decoding one

The audit is payload-free, which is why it is fast. Data age is the first thing that needs
a field out of the message, so it buys exactly one and nothing else. In ROS 2 CDR a
message whose first field is a `std_msgs/Header` puts `sec` and `nanosec` immediately
after the 4-byte encapsulation header, so reading a stamp is an 8-byte `struct.unpack`
rather than a deserialization.

That is a claim, so it is checked rather than assumed:
`scripts/verify_stamp_peek.py` decodes every message properly and compares. **134 topics
across 11 real ROS 2 recordings, zero disagreements.** Re-run it before trusting the peek
on a new corpus; it exits non-zero if any topic disagrees.

**The gate is the schema, never the bytes.** `std_msgs/Float32` has no header, and its
first eight bytes unpack perfectly happily into a plausible-looking stamp. The decision is
made once per schema from the message definition; a schema whose first field is not a
Header (or a bare `Time`) is never peeked at all, and its topic is reported unmeasurable.

### The four things it refuses to call an age

Each of these was cheaper to report honestly than to paper over, and three of the four
were found on real recordings rather than imagined:

| Situation | What a naive version does | What this does |
|---|---|---|
| No `header` in the schema (`geometry_msgs/Twist`) | substitutes arrival time | names the topic **unmeasurable** |
| `header.stamp` present but never set | reports data 55 years old | reports *"has a stamp but never sets it"* |
| Stamps from a steady clock, not the epoch (`/bond`) | reports data 54 years old | reports *"on a different clock"*, excludes it |
| Publisher clocks disagree | reports skew as though it were latency | withholds every age and says why |

The third is the one worth dwelling on. On a real shuttle-bus recording `/bond` stamps
from a monotonic clock that starts near zero; differenced against a wall-clock publish
time, that is an age of 1,725,326,040 seconds. One such row averaged into a report makes
every honest number next to it look arbitrary — so an implied age beyond a plausible bound
is treated as evidence of two clocks, not of old data.

### Measured

| | Precision | Recall |
|---|---:|---:|
| Synthetic pipelines | 1.000 | 1.000 |
| **Injected into real recordings** | **0.900** | **0.750** |

Read the real row with its case table, not on its own. Every fault at 2×, 4× and 8× the
target topic's own noise band was caught, on all three recordings — 9 of 9. Every fault at
1× — exactly the size of the variance it hides in — was missed, 0 of 3, and those three
misses are the whole of the recall shortfall. A detector that fired there would be
reporting the recording's own jitter as a fault.

One rule earned along the way: **a bucket needs 100 age samples before its P99 is a
statistic rather than a maximum.** Without that floor, `nuway_stops` — the parked shuttle
bus whose topics are event-driven — produced 16 false "data age is growing" findings, one
claiming a 57× rise on a topic publishing at 0.4 Hz. Sparse topics now get their age
reported and their *trend* refused, flagged as `trend_assessable: false` so that a skipped
check cannot read as a passed one. That is W15's lesson applied to a new detector.

Details in [`DATA_AGE.md`](../evals/age/DATA_AGE.md); regenerate with
`uv run python -m evals.age.data_age`.

## The pre-flight gate: refuse to start a mission that is already broken

A field test day costs thousands and gets burned because a node did not launch, a sensor
came up in the wrong mode, the clock was not synced, or a topic was already degrading
before anyone drove anywhere. It is discovered that evening, in the bag.

```bash
baglens preflight --record --from known_good.mcap --out fleet_baseline.json
baglens preflight --expect fleet_baseline.json --for 30s
```

Thirty seconds, then one answer: **GO** or **NO-GO**, with reasons and an exit code.

```
NO-GO — 5,549 messages in 0.4s

2 reason(s) not to fly:
  FAIL  /scan: 150 of ~300 expected messages (50%); silent for 0.0s of 30s
  FAIL  /scan: 5.02 Hz vs 10.02 Hz baseline (-50%)

topic_present  4 pass
coverage       3 pass, 1 fail
rate           3 pass, 1 fail
data_age       4 pass
clock          1 pass
degrading      1 pass
tf             1 unchecked
    ?  transform-tree completeness is not yet implemented (F3)

1 item(s) could not be checked in this window. They are not passes.
```

**The baseline is captured, never hand-written.** `--record` derives it from a run someone
was willing to call good, because the rate that matters is the one this robot actually
achieves, not the one on the sensor's datasheet. A topic the audit could not measure is
left out, so the gate reports it unchecked rather than comparing against a guess.

**Rate alone is not fitness to fly.** `observed_hz` is the *modal* rate and is robust to
gaps by design — so a topic silent for twenty of thirty seconds still reports its nominal
10 Hz and sails through. The gate therefore counts the messages that actually arrived
against the messages the baseline implies. That check is what catches a node that dropped
out and came back, and it was added because a 20 s dropout was waved through without it.

**Three statuses, not two.** Thirty seconds is shorter than cadence warmup on a slow
topic, so some things genuinely cannot be judged. Those are `unchecked` — listed in the
verdict, never counted as passes — which is `unassessable` applied where the cost of
getting it wrong is a lost field day. `--strict` makes them fatal. TF completeness is
F3's and is not built, so the gate says so by name rather than quietly omitting it.

### Measured

From `tests/integration/test_preflight.py`, which is the harness:

| | Result |
|---|---|
| Healthy graphs, 10 seeds | **0 false alarms** |
| Missing topic | caught, names `/scan` |
| Halved rate | caught, names `/scan` |
| Topic silent 20 s of 30 | caught, names `/scan` |
| Clock skew | caught |
| Already degrading | caught, names `/camera/image_raw` |
| Verdict latency | **< 35 s** for a 30 s window |

The false-alarm row is the one that decides whether anyone leaves the gate switched on.
A gate that cries wolf gets disabled, and a disabled gate is worse than no gate because
everyone still believes it is running.

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

MB/s is dominated by *message count*, not bytes: the audit does not parse payloads, so a
bag full of camera frames audits far faster per megabyte than tiny synthetic messages do.

### What data age costs

"Never parses payloads" stopped being exactly true when data age landed, so here is the
measurement rather than a reassurance. On `nuway_waypoints.mcap` (200,144 messages),
best of two, via `scripts/bench_stamp_peek.py`:

| | msg/s | vs payload-free |
|---|---:|---:|
| Arrival scan, payload-free | 67,019 | — |
| Arrival scan, with the stamp peek | 60,574 | +10.6% time |
| Full audit, without `data_age` | 21,694 | — |
| Full audit, with `data_age` | 13,678 | +58.6% time |

The peek itself is cheap, as designed — 10.6% for an 8-byte `unpack` on every headered
message. The rest is the detector: four streaming quantile estimators per message, in
Python. **That is a real cost and it is on by default**, on the argument that a feature
nobody switches on is a feature nobody has. Turn it off with
`--detectors cadence,gap,rate_degradation,jitter,dropped,clock,correlation,file_integrity`,
and the numbers above are what you get back.

The obvious optimisation was tried on paper and rejected: replacing the three streaming
quantile estimators with one fixed-bin log histogram would be much cheaper per message,
but 256 bins cost 2,048 B per topic — the entire per-topic state budget, spent on one
estimator. The P² markers cost ten numbers and land the whole detector at **1,040 B per
topic**. Runtime was the cheaper thing to give up.

Two things that do *not* pay this cost: ULog, whose reader never offers stamps, so the
PX4 column above is unchanged; and any topic whose schema has no header, which is skipped
before a byte is read.

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
