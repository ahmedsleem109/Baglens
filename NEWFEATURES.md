# NEWFEATURES — from "was my recording OK?" to "is my robot OK, and which part isn't?"

**Read this before implementing anything.** `PHASE3.md` is finished — M1–M4 landed on
2026-08-16 and only its M5 release steps remain, all of which need a human. This file is
what to build next and in what order. `ROADMAP.md` is the history and the reasoning.
`ENHANCEMENTS.md` remains out of scope.

---

## Why these four, and why now

The tool as it stands audits whether a *recording* can be trusted. That is real and it is
measured — 0.824 recall against faults injected into real recordings, 0.955 precision
against PX4's own labels — but it is a small problem. Roughly one flight in five has a
recorder stall at all, costing 0.8% of recording time. It saves an engineer some
confusion. It does not save their week.

These four move the tool to the failures that *do* cost a week, and they reuse the
machinery already built rather than starting again: streaming detectors with bounded
state, a live path byte-identical to the offline one, alert semantics that do not flap,
and provenance on every claim.

**The recording-integrity work is not discarded — it becomes the foundation.** A tool that
cannot tell its data is broken will confidently misdiagnose the robot, which is the exact
failure this project exists to avoid. `unassessable` is what keeps every feature below
honest.

### These are not speculative

Every one of the four was picked from general robotics experience and then **confirmed by
the author as something they have personally hit.** That is a stronger basis than anything
in `PHASE3.md` started with — but it is still one engineer.

**Each feature below has an empty `The story` block. Fill it in before building that
feature.** A concrete account of the day it cost you is worth more than the pitch: it
names the exact signal that would have caught it, it becomes the synthetic fixture, and it
becomes the section in the README that makes a stranger recognise their own week. A
feature whose story cannot be written is a feature nobody needed.

---

## The constraint that still must not be violated

> **Detectors must be single-pass with bounded state — no exceptions.**

Fixed-size state per topic, serialisable, checkpointable, the same code path whether fed
from a file or a live subscription. This bought the entire live path for one 250-line
file; do not spend it. Welford and EWMA, never `numpy.mean` over an accumulated array.
Thresholds from a warmup window, never from global file statistics. Ring buffers and
fixed-bin histograms, never growing lists.

If an offline formulation is the only one you can see, say so and stop. Do not ship the
offline version with a TODO.

**A second constraint now joins it.** The audit is currently *payload-free* — it reads
arrival times and never decodes a message, which is why it sustains 44,500 msg/s and why
an 886 MB bag audits at 501 MB/s. F1 breaks that for the first time. See its design notes:
the answer is to peek at a fixed offset, not to deserialize.

---

## Build order

**F1 → F2 → F3 → F4.** Chosen by the author. The reasoning that supports it:

1. **F1 (data age)** produces a number teams actively want and currently cannot get. It
   also forces the payload-reading question, and every later feature is easier once that
   is settled.
2. **F2 (pre-flight)** is mostly assembly of things that already exist, so it ships fast
   and is the first feature that prevents a failure rather than explaining one.
3. **F3 (TF integrity)** is self-contained and high-frequency pain.
4. **F4 (node attribution)** is last because it is the most speculative in
   implementation — it needs a node→topic mapping that is easy live and awkward from a
   file.

**One at a time.** Do not scaffold all four. Four plausible-looking implementations with
no idea which of them work is the failure mode this project already avoided once.

For each feature, in this order:
1. Write its synthetic fixture generator *before* the detector, extending
   `tests/synth/generate.py`.
2. Write the detector.
3. Produce a precision/recall number against the fixtures, then against a real recording
   from `~/data/public/ros2` with faults injected by `tests/synth/inject.py`.
4. Only then move on.

---

# F1 — End-to-end data age

**The number that decides whether a robot is safe, and nobody measures it.**

### The pain

The question that matters is not "is `/camera` publishing at 30 Hz?" It is **"how old was
the camera frame that produced this steering command?"** Every team hand-rolls this
badly or never measures it at all. When it drifts from 80 ms to 300 ms the robot gets
quietly worse, the behaviour looks like a tuning problem, and nobody can say which stage
grew.

### The story

> *(Fill this in before building. When did stale data bite you? What did it look like from
> the outside — did the robot overshoot, oscillate, react late? How long did it take to
> find? What would have told you in one line?)*

### What it does

ROS messages carry `header.stamp` — the time the data was *captured* — and that stamp
propagates through the pipeline as nodes pass derived results along. Following it gives
the true age of the information behind every command, per stage:

```
/camera/image_raw   stamp=t          published t+12ms   →  capture→publish  12 ms
/detections         stamp=t          published t+94ms   →  perception       82 ms
/cmd_vel            (from stamp t)   published t+131ms  →  planning         37 ms
                                                           end-to-end      131 ms
```

Report the distribution, not the mean: P50/P95/P99 per stage, and the trend. A P99 that
doubles over a mission is a finding even when the mean does not move.

### Why baglens can do this

It is the natural extension of the clock work (`clock.py` already reasons about
`log_time` vs `publish_time`), the provenance model already carries time ranges, and the
streaming discipline means the percentile tracking has to be bounded — which is a solved
problem (P² or a fixed-bin log histogram, both already in `detectors/base.py` territory).

### Design notes, and the honest problems

- **Payload reading, cheaply.** Full deserialization would destroy the throughput this
  project measured and published. In ROS 2 CDR a message whose first field is a
  `std_msgs/Header` puts `sec` (int32) and `nanosec` (uint32) at a fixed early offset,
  immediately after the 4-byte encapsulation header. **Verify this on real data before
  building on it** — check against `/camera/image_raw` and `/scan` in
  `~/data/public/ros2` — but if it holds, reading a stamp is an 8-byte peek, not a decode.
- **Not every message has a header.** `geometry_msgs/Twist` has none, so the chain breaks
  exactly at the actuator, which is where you most want it. Handle `TwistStamped` where it
  exists, and where it does not, say so rather than silently substituting arrival time.
  **An unmeasurable stage must report as unmeasurable** — that rule is the whole moat.
- **The propagation graph is inferred, not declared.** Nobody tells you `/detections`
  derives from `/camera`. Infer it from stamp equality: if a `/detections` message carries
  a stamp that exactly matches an earlier `/camera` message, that is a causal link with a
  very low false-positive rate. Stamps that match nothing are their own finding — a node
  that restamps with "now" has destroyed the trace, and that is worth reporting.
- **Clock skew is a confound.** If the sensor and the compute have different clocks, data
  age is measured across two clocks and is meaningless. `clock.py` already detects this;
  gate the finding on it.

### How it is measured

`tests/synth/generate.py` gains a pipeline fixture: a chain of topics that propagate a
stamp with a configurable per-stage delay, and a `stale_pipeline` fault that grows one
stage's delay over the run. Precision/recall against those, then injection into a real
recording.

### Targets

Report end-to-end and per-stage P50/P95/P99 on a real ROS 2 recording, with each
unmeasurable stage named. Detect an injected stage-delay ramp at ≥0.90 recall.

---

# F2 — Pre-flight readiness gate

**Refuse to start a mission that is already broken.**

### The pain

A field test day costs thousands and gets burned because a node did not launch, a sensor
came up in the wrong mode, the clock was not synced, or a topic was already degrading
before anyone drove anywhere. It is discovered that evening, in the bag.

### The story

> *(Fill this in before building. Which field day did you lose, and to what? What was the
> one thing that, checked in the first 30 seconds, would have saved it?)*

### What it does

```bash
baglens preflight --expect fleet_baseline.json --for 30s
```

Watches the **live** graph for thirty seconds and answers one question: is this robot fit
to record a mission? Green or red, with reasons and an exit code.

- Every expected topic present and publishing.
- Rates within tolerance of the baseline.
- TF tree complete (see F3).
- Clocks consistent across publishers.
- Nothing already degrading — the D3/D4 leading indicators, on a live stream.
- Data age within budget (F1), so a pipeline that is already lagging is caught before it
  matters.

The baseline is not hand-written: `baglens preflight --record` captures it from a run that
was known good, which is `to_state()` on the auditor plus the topic inventory.

### Why baglens can do this

Almost entirely assembly. `live.py`, `ros2.py`, `alerts.py` and the cadence detectors
already exist and are verified. This is the feature that most directly answers *"inspect
the failure before it happens"*, and it is closest to shipping.

### Design notes

- **Thirty seconds is shorter than cadence warmup on slow topics.** Some topics will not
  have a baseline in that window. Report them as unchecked rather than as passing —
  `unassessable` applies here exactly as it does everywhere else.
- **It must be fast to say yes.** A gate that takes five minutes gets skipped, and a
  skipped gate is worse than none because it creates false confidence.
- **Exit code, JSON, and one screen of text.** It has to work from a launch file, a CI
  job, and a human at a laptop in a field.

### How it is measured

A stub graph like `tests/integration/test_ros2.py` uses: launch a synthetic graph with a
topic missing, a topic at half rate, a skewed clock. The gate must catch each and must not
fire on a healthy graph — the false-positive rate on the healthy case is the number that
decides whether anyone keeps it switched on.

### Targets

Zero false alarms across ten healthy synthetic graphs. Catches missing topic, halved rate,
clock skew, and already-degrading rate. Verdict in under 35 s wall clock.

---

# F3 — Transform integrity

**The TF failures that waste the most hours, including the silent ones.**

### The pain

*"Lookup would require extrapolation into the future"* is among the most-cursed errors in
ROS. The loud ones cost an afternoon. The silent ones cost a week: two nodes publishing
the same transform and fighting each other, a static transform nobody ever published, TF
timestamps ahead of the sensor data they are supposed to align, a tree that is complete
only intermittently.

### The story

> *(Fill this in before building. Which transform, which two nodes, how long did it take,
> and what did the robot appear to be doing wrong while the real fault was in TF?)*

### What it does

- **Duplicate publishers** of the same parent→child transform, with the node names and
  how often they disagree. This one is silent and vicious.
- **Staleness relative to consumers**: a transform older than the sensor data using it.
- **Extrapolation risk**: how often a lookup at a sensor's stamp would fall outside the
  available TF window, before it becomes a runtime error.
- **Intermittent tree completeness**: the chain base→map exists 94% of the time, and the
  6% is a finding.
- **Rate and jitter on TF itself**, which the existing detectors already provide once
  `/tf` is treated as the structured stream it is rather than as one opaque topic.

### Why baglens can do this

`spatial.tf_tree_health` already exists as a starting point, `/tf` and `/tf_static` are in
every ROS 2 recording, and the failure class is precisely the one this project is built
around: **the data looks fine and is quietly wrong.**

### Design notes

- **`/tf` is many streams in one topic.** Each message carries a list of transforms, so
  per-topic cadence machinery has to be applied per parent→child pair. That is more state
  than one topic's worth — bound it explicitly and report the truncation, the way D2 and
  D7 already do.
- **This requires payload reading**, so it depends on F1 settling that question. TF
  messages must be decoded properly, not peeked; budget for it and measure the cost.

### How it is measured

Fixtures with: two publishers of one transform, a static transform that never arrives, TF
stamped 200 ms ahead of the sensors, and an intermittently broken chain. Each must be
caught by name, and a healthy tree must produce nothing.

### Targets

≥0.90 recall on each of the four fixture faults, zero findings on a healthy tree.

---

# F4 — Node attribution

**Turn "topic X is jittery" into "node Y is starving."**

### The pain

In ROS 2 one slow callback starves an executor and silently degrades every topic that node
publishes. Symptoms appear across the system; the cause is one node. Teams lose days
walking backwards from a symptom to a cause that a machine could have named.

### The story

> *(Fill this in before building. Which node, what was it doing, how did the symptom
> present, and how long before someone thought to look at the executor?)*

### What it does

Today: *"`/scan` is jittery."*
After: *"`perception_node`'s executor is starving — all four of its topics degraded
together, in publish order, while topics from other nodes did not."*

### Why baglens can do this

`CorrelationDetector` already answers "which topics degraded together, and does that
pattern imply a shared cause?" Going from *the recorder stalled* to *this node's executor
stalled* is the same machinery pointed one level down — and the W15 work means the
concurrency scoring is now measured and trustworthy rather than assumed.

### Design notes, and why this is last

- **The node→topic mapping is easy live and awkward from a file.** `rclpy` gives it
  directly via the graph API; a recorded bag usually does not carry it. Options: capture
  it in F2's baseline, read it from `/rosout` and `/parameter_events`, or accept that this
  feature is live-first and file-second. **Decide this before writing code** — it changes
  the shape of everything.
- **Starvation has a signature**: same-node topics degrade together *in publish order*,
  because a starved executor still services callbacks in sequence. That ordering is the
  discriminator against a shared bus or a shared sensor, and it is what makes this a
  detector rather than a grouping.
- Distinguish executor starvation from CPU saturation from a blocked I/O callback if you
  can; if you cannot, say which of them it might be rather than picking one.

### How it is measured

A synthetic multi-node graph where one node's callbacks are artificially delayed. The
detector must name that node and must not accuse its neighbours.

### Targets

Correct node named in ≥0.85 of injected starvation cases, with under 0.10 misattribution
to an innocent node. Misattribution is the metric that matters — accusing the wrong node
is worse than saying nothing, because it sends someone down a two-day path.

---

## Rules carried forward, all of them earned

1. **Re-run the labelled corpora after any detector change, including performance-only
   ones.** `uv run python scripts/split_false_positives.py --dir ~/data/public/px4`
   (~25 min) and `uv run python -m evals.integrity.injected --bags ~/data/injected`.
   A bounded-state cap added purely for memory once cost 35 of 152 labelled dropouts while
   `pytest` and both synthetic evals stayed green.
2. **A published number with no script behind it is a guess.** Both figures that turned out
   wrong were the two with no harness regenerating them.
3. **A measurement taken while another bug is live can convict the wrong change.** The W15
   fix sat rejected for two sessions on a number a different bug had produced. When a fix
   is rejected on evidence, re-run that evidence after the next bug is found.
4. **Numbers measured on a selected subset are flattering until re-run on everything.**
   0.832 on twelve hand-ranked flights became 0.381 on all 105.
5. **Never tune against unlabelled data.** It is scoring the detector against its own
   author.
6. **A label the detector cannot satisfy measures the recording, not the detector.** And a
   label that changed nothing is worse than no label at all.
7. **Never hand-edit a generated file.** Regenerate it or leave it stale and say so.
8. **When a rule about what the tool refuses to judge is added, grep for every detector
   that should obey it.**

## What not to do

- Do not add a machine-learned model. You have 186 real labels; that is enough to
  *evaluate* and nowhere near enough to *train*. Legibility is the product — the score
  formula is published on purpose, and a learned model cannot be argued with.
- Do not take automatic action on the robot. At 0.955 precision, one action in twenty is
  taken on a fault that was not there, and the moment you act you own the outcome. Be the
  sensor for someone else's actuator: emit a signal good enough for Nav2, a lifecycle
  manager or a safety PLC to act on.
  *(The one safe exception, worth building eventually: acting on the **recording** — shed
  a low-priority topic when the recorder is about to saturate. The worst case of a false
  positive there is that you recorded slightly less.)*
- Do not build the industrial/PLC adapter yet. The detector maths transfers cleanly to
  OPC UA — a tag polled every 100 ms is a topic at 10 Hz — but it needs a plant or a
  simulator to test against, and it is a different market with a different sales motion.
  Note it, do not start it.

## Environment

Develop and run in **WSL, not Windows**. Python via `uv`. Check `df -h /mnt/d` before
downloading anything: the WSL ext4 image lives on the Windows D: drive, and filling it
once remounted the image read-only mid-write and the distro would not boot.
