# Enhancements — where the defensible product is

**This is a positioning document, not a backlog.** `ROADMAP.md` is what we owe the
current design. This is the argument for what the product should become, written from
the point of view of the person we want to reach: someone whose hardware is failing in
the field this week.

---

## 0. The problem with being a rosbag debugger

There are a thousand tools that open a bag and show you what's in it. Foxglove is better
at that than we will ever be, it's free, and it has a company behind it. Competing on
"inspect a recording" is competing on a solved problem against a better-funded incumbent.

The trap is subtle, because our current framing invites it. Every tool in this category —
including ours today — takes **one file** as its unit of value. You give it a file, it
tells you about that file. That framing caps the product at "debugger," and debuggers are
commodities.

**The unit of value that isn't commoditised is a vehicle over time.**

Nobody has a crisp answer to these, and every hardware team asks them weekly:

- *Is SN-0043 getting worse?*
- *Did firmware 2.4.1 make the fleet worse, and on which units?*
- *This one crashed — did the earlier flights already show it coming?*
- *Which of my 8,000 recordings are clean enough to train on?*
- *Should this vehicle fly today?*

None of those questions can be answered by opening a file. All of them can be answered by
a system that has audited every file, cheaply, with the same detectors, and kept a
bounded summary per mission. **We already built that system and are describing it as a bag
auditor.**

> **The repositioning, in one line:**
> From *"audit this recording"* to *"here is the reliability evidence for this fleet."*

Everything below follows from that.

---

## 1. What we already have that others don't — protect these

Before adding anything, be clear about the four things that are genuinely hard to copy.
Every feature in this document should compound one of them.

| Asset | Why it's hard to copy | Where it lives |
|---|---|---|
| **Single-pass, bounded-state detectors** | The same code runs on a 50 GB file and on a live subscription. Anyone who wrote the offline version first cannot get here without a rewrite. | `detectors/`, ~2 KB state/topic |
| **Serialisable detector state** | A mission compresses to a fixed-size struct. That is what makes fleet-scale and on-device both affordable. | `to_state()`/`from_state()` |
| **Caveats, not just findings** | Telling an agent *what it must not conclude* is the thing that stops confident-and-wrong analysis. Nobody else emits this. | `HealthReport.caveats` |
| **Provenance on every claim** | We can measure our own hallucination rate. It's the axis nobody else publishes. | `provenance.py`, `evals/scoring.py` |

And the one earned this month: **real labels.** PX4 logs carry the flight controller's own
dropout records, so we can score detectors against ground truth we didn't write
(`evals/integrity/REAL_DATA.md`). That's a validation moat — it means our numbers are
falsifiable and a competitor's marketing claims aren't.

**The strategic point about bounded state:** a mission's detector state is ~2 KB per topic.
A 100-topic vehicle is ~200 KB per mission. Ten thousand missions is 2 GB — a laptop.
This is why fleet-scale analytics is cheap *for us* and expensive for anyone who has to
re-read the bags. Do not give this up for convenience.

---

## 2. Tier A — the features that make the tool unignorable

Ranked by how likely each is to make an engineer say *"I need this in my workspace."*

### A1. Precursor mining — "the last three flights already showed it" ⭐ the killer app

**The pitch:** A vehicle crashed. Feed us the crash and the fleet history. We tell you
which earlier missions already carried the signature, and what the earliest detectable
warning was.

This is the question every hardware team actually cares about and no tool answers.
Post-incident, they hand-scroll through plots looking for "something that looks off."

**How it works with what we have:**

1. Reduce each mission to a fixed-length **health signature** — per-topic rate drift,
   jitter CV trend, drop rate, clock behaviour, correlated-stall count. This is mostly
   already computed; it just needs to be a vector, not prose.
2. Label missions by outcome. **PX4 gives this away free** — flight reviews carry crash
   and error flags, so we can build a labelled precursor set without a single customer.
3. Fit something deliberately simple and inspectable (nearest-neighbour, or a logistic
   model on a dozen features). Not a neural net — an engineer must be able to argue with
   the answer.
4. Output: *"This flight's signature is closest to 3 missions that ended in ESC
   desync. The shared feature is `/esc_status` jitter CV rising 2.4× over the last
   4 minutes. That signal was present 2 flights before the failure."*

**Why it wins:** it converts us from forensics (after the crash) to prediction (before
it). That is a different budget line — maintenance, not tooling.

**Honest difficulty:** medium-high, and it needs the labelled corpus first. The failure
mode is overclaiming: with n=50 crashes you have a suggestion engine, not a predictor.
Ship it with calibrated confidence and an explicit "this is a lead, not a diagnosis."

### A2. Per-unit hardware fingerprinting — "SN-0043 is drifting"

**The pitch:** Track health *per physical vehicle*, not per file. Surface the unit whose
sensors are degrading before it fails.

Fleet operators today have no per-airframe reliability picture. They know a drone crashed;
they don't know it had been getting worse for six weeks.

- Group missions by vehicle ID (PX4 has one; ROS 2 needs a convention).
- Trend each topic's jitter, drop rate, and clock behaviour *across missions*.
- Fire on a sustained per-unit regression: *"SN-0043: `/sensor_baro` variance up 3.1× over
  its own 20-flight baseline. Fleet median unchanged — this is the unit, not the build."*

**The critical detail — compare a unit against itself.** Every airframe has a different
normal. A fleet-wide threshold produces noise; a per-unit baseline produces signal. This
is the same warmup-learns-normal principle the detectors already use, lifted one level.

**Why it wins:** this is predictive maintenance, and it's the feature an operations lead
buys rather than an engineer. Directly reuses `to_state()`.

### A3. Fleet regression detection — "git bisect for firmware"

**The pitch:** *"Health regressed at firmware 2.4.1, on the 12 units running the
CUBEORANGE board, and here's the topic."*

Correlate mission health against firmware version, hardware revision, config parameters,
and environment. Report the split that best explains a regression.

This is the single most requested thing that doesn't exist. Robotics teams ship firmware
with no equivalent of a performance regression test, so degradations are found weeks later
by a customer.

- Metadata already exists (PX4 logs carry version + board; rosbag2 has it in metadata).
- The stats are ordinary — group, compare distributions, rank by effect size.
- Present as a bisect, not a dashboard: **one sentence naming the likely cause.**

**Why it wins:** it makes us part of the release process, not the debugging process. Tools
used before shipping get budget; tools used after an incident get tolerated.

### A4. Pre-mission gate — "should this thing fly today?"

**The pitch:** One command, one answer, in CI or on the bench.

```
$ baglens preflight --vehicle SN-0043
HOLD  — /sensor_baro variance 3.1× this unit's baseline across the last 4 flights.
        Trend began 2026-08-02. Similar to 2 units that later failed baro-altitude hold.
        Override: --force
```

Reduce everything above to a **go / no-go with a reason**. This is where the product
stops being analysis and becomes a control. It's also the natural on-device form (Tier 3.1
in the roadmap), and the demo that makes people lean forward.

**Why it wins:** an engineer will integrate a thing that emits a decision. They will
*intend* to integrate a thing that emits a report.

---

## 3. Tier B — the features that make it sticky

Tier A wins the demo. Tier B is why it's still installed in six months.

### B1. Training-data curation for robot learning ⭐ possibly the largest adjacent market

Robot-learning teams (LeRobot, imitation learning, VLA models) train on tens of thousands
of recorded episodes and have **no idea which ones have holes in them**. They are
currently training on data with 8% dropout and silently degrading their models.

We already produce exactly the artifact they need — a per-window statement of what the
data can and cannot support.

- `dataset.curate` — filter a corpus by integrity score and per-topic caveats.
- Emit a **dataset card**: episode count, what was excluded and why, per-topic coverage.
- Export the caveat windows as a mask so a dataloader can *skip the compromised spans*
  rather than dropping whole episodes.

**Why it wins:** it's a different buyer (ML, not hardware) reached with the same engine,
and "your training data has holes and here's the proof" is a demo that lands in seconds.
The `caveats` field was built for exactly this and is currently underused.

### B2. Root-cause correlation with system telemetry ✅ BUILT — and the answer was "no"

Right now we say *"the recorder stalled."* The next question is always *"why?"*

This section originally asserted the answer:

> ~~*"All 54 logging blackouts coincide with `cpuload` > 0.92 and follow an SD write
> burst. This is storage backpressure, not a sensor or a CPU-bound node."*~~

**That was invented, and measuring it refuted both halves.** Across 103 real flights and
1,935 blackouts, CPU load is if anything marginally *lower* before a stall (*d* = −0.21,
sign agreement 57%), message volume shows no burst (*d* = −0.08), and PX4's own
`logger_status` backpressure telemetry is absent from every log. Full study:
[`evals/integrity/STALL_ATTRIBUTION.md`](evals/integrity/STALL_ATTRIBUTION.md).

What shipped is therefore an **attribution engine, not a stated cause**
(`kernels/attribution.py`, `health.explain_stalls`): it ranks candidate signals by effect
size, demands that a signal shift consistently before *most* individual stalls rather than
just move an aggregate mean, and returns `unexplained` / `no_data` as first-class
verdicts. On this corpus that is the honest answer for 100 of 103 flights.

The one thing it *can* say is the temporal pattern: blackouts are strongly clustered,
which rules out the per-sensor explanations an engineer would otherwise chase first.

**The transferable lesson for everything else in this document:** the consistency
requirement cut false attributions from 12 flights to 3. Any feature here that ranks
causes across a fleet will hit the same multiple-comparisons trap, and "it found
something" is not evidence until the something survives being asked to recur.

### B3. Sensor cross-validation

The detectors currently reason about *timing*. The next tier is *consistency between
sensors that should agree*:

- IMU vs GPS velocity divergence
- redundant IMU/mag/baro disagreement (PX4 logs 3 of each)
- EKF innovation test ratios trending up — the estimator's own "I don't believe this
  sensor" signal, already in `estimator_innovation_test_ratios`

This catches the failure mode timing analysis structurally cannot: a sensor that keeps
publishing perfectly on schedule and is **lying**. A stuck baro at a constant value has
flawless cadence.

**Keep the streaming constraint.** All of these are expressible as bounded-state online
comparisons; do not let this become the excuse that buffers the file.

### B4. Incident report generation

One command, one shareable document: timeline, findings ranked by severity, the co-silent
topic list, the lag curve, and an explicit "what this data cannot tell you" section.

Low technical difficulty, disproportionate adoption effect — it's the artifact that gets
forwarded to a manager or a customer, which is how tools spread inside a company.

### B5. Regression gate as CI

A GitHub Action that audits the bag produced by a simulation run and **fails the PR if
health regressed against the baseline**. Combined with A3, this is the robotics equivalent
of a performance test — a category that essentially doesn't exist.

---

## 4. Tier C — adjacent, larger, slower

Real markets, but each is a company-shaped commitment rather than a feature.

- **Certification and compliance evidence.** BVLOS drone ops, DO-178C, ISO 26262 all
  require *evidence* that recorded data is trustworthy. Our provenance chain is already
  most of the way there. Tamper-evident audit trail + signed reports. High-value,
  slow-moving, regulated buyer.
- **Field-deployed edge daemon.** The full payoff of the streaming architecture: watch a
  live robot, emit degradation warnings over a constrained link. Send the 2 KB state, not
  the log. This is Tier 3.1's real destination.
- **Insurance and warranty analytics.** Fleet reliability evidence has an obvious buyer
  outside engineering. Mentioned for completeness; a distraction until Tier A works.

---

## 5. Deliberately not doing

Saying no is what keeps the above coherent.

- **Not a visualiser.** Foxglove wins. Hand off to it — deep-link *into* Foxglove at the
  exact timestamp of a finding. Being the tool that tells Foxglove where to look is a
  better position than competing with it.
- **Not a message-decoding library.** The payload-free design is why we're fast. Decoding
  arbitrary types would cost the performance story for little gain.
- **Not a cloud platform.** Local-first, zero telemetry. For hardware teams whose flight
  logs are competitively sensitive, "your data never leaves" is a *feature*, and it's one
  a SaaS competitor structurally cannot match.
- **Not a general anomaly-detection framework.** Opinionated detectors that name the
  physical cause beat a configurable engine that outputs "anomaly score 0.73."

---

## 6. Sequencing — what to actually do

The ordering matters more than the list. Two of these are prerequisites, not features.

**~~First, and blocking everything: fix the score.~~ Done** (Tier 1.5 in `ROADMAP.md`).
Every real flight used to audit as `compromised`; a 25-flight sample now reads 4
trustworthy / 12 usable / 9 compromised, and the nine lost a mean 31.5% of their
recording. Fleet analytics needed this first — a dashboard built on a metric that
saturates at "everything is broken" would have shown every unit red.

**~~Second: B2 (root-cause correlation).~~ Done — see above.** It did not produce the
actionable root cause predicted, because there wasn't one to find. It did produce a
reusable attribution engine and a calibration lesson that the fleet features will need.

**Next: A2 (per-unit fingerprinting).** The smallest step from "audit a file" to "track a
vehicle," and it reuses `to_state()` directly. This is the repositioning made real.

**Then: A4 (pre-mission gate)** as the demo, and **A1 (precursor mining)** as the headline
once enough labelled data exists.

**B1 (dataset curation)** can run in parallel — different buyer, same engine, and it needs
nothing from the fleet work.

---

## 7. The honest risk

Every item here is more valuable than another detector, and every one is a bigger
commitment than it looks. The specific failure mode to watch for is **building A1 before
the scoring is fixed and the corpus is large** — a precursor engine trained on 50 crashes
with a saturated health metric will produce confident nonsense, and confident nonsense
from a trust tool is worse than no tool.

The thing that makes this project credible is that it publishes its own false-positive
rate and says plainly what it cannot support. Keep that discipline as the surface grows,
because it is the actual product.
