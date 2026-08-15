# Phase 3 — the spec

**Read this before implementing anything.** `ROADMAP.md` is the history and the reasoning;
this file is what to do next and in what order. `ENHANCEMENTS.md` is explicitly out of
scope and should not be used to plan work.

The ordering is by **dependency, not by appeal**. Each phase is unsafe to start before the
one above it is done, and the reason is given in each case. The most tempting mistake is
to start Phase 2 (hardware) or Phase 3 (fleet) while Phase 1's numbers are still wrong —
both would be built on a detector that is wrong two times out of three, and both would
have to be rebuilt.

---

## State as of 2026-08-14

**Green:** 188 tests + 2 skipped, `ruff`, `mypy` (configured). 43 MCP tools, 8 detectors,
4 formats.

**Measured, real:** recall **1.000**, precision **0.381** for `correlation` against PX4's
own dropout records across **105 distinct flights** (677 minutes, 152 labelled dropouts).
`evals/integrity/REAL_DATA.md`.

**Measured, synthetic:** 1.000 precision/recall across 8 fault classes, 0.000 false
positives on 20 clean bags.

**Live path works and is verified.** `src/baglens/live.py` +
`tests/integration/test_live.py` + `scripts/live_feed.py`. Live and offline produce
byte-identical verdicts, scores, findings and per-topic scores on three real PX4 flights
across 34 mid-stream snapshots. The detectors required no changes.

### Landed in the last session

| Fix | Evidence |
|---|---|
| Correlation denominator floored on declared topic count | `fddab288` 0.0 → 81.2; full-corpus precision 0.365 → 0.381, recall unchanged |
| ULog timestamp `0` treated as unset, not as t=0 | two flights read 56 years; now 214.5s / 584.7s; 79 of 121 files affected |
| `explain_finding` no longer clobbers a finding's own evidence | a 6.31s stall was reporting the recording's 311.97s under `duration_s` |
| Caveats grouped by template | 65 → 17 on `588ff157` |
| `find_gaps` honours `unassessable` | `/event`'s phantom 131s / 121,936-message gap no longer tops the list |
| Budget trim elides on a word boundary | interpretations no longer end mid-word |
| Live monitoring | new; see above |

---

## The data you have, and whether it is enough

| Asset | Size | Enough for |
|---|---|---|
| `~/data/public/px4` — 121 `.ulg`, 4.1 GB, **105 distinct flights**, 677 min | large | PX4 detector scoring, live-feed testing, corpus statistics |
| **152 labelled dropouts** (PX4 logger's own records) | small but *real* | the only ground truth here nobody in this repo wrote |
| Synthetic fixtures — 16 conftest fixtures across 8 fault classes, plus clean / growing / corrupt / truncated | adequate | unit and integration testing, regression gates |
| 20 clean synthetic bags | adequate | false-positive rate on known-good input |
| 56 tool-surface eval cases (`evals/cases/*.yaml`) | adequate | MCP surface behaviour |
| 10 golden snapshots | adequate | output drift |

**Verdict: sufficient to start Phase 1, and not sufficient to finish it.** Two gaps:

1. **There is not one real non-PX4 recording on this machine.** `find ~ -name '*.mcap' -o
   -name '*.db3' -o -name '*.bag'` outside `tests/` returns nothing. Every claim about
   ROS 2 rests on synthetic fixtures this repo generated — which can only prove that the
   detectors and the generator agree. ROS 2 is the actual target market. **This is the
   single most important data gap and it is P1.3.**
2. **The only real labels are recorder dropouts.** Nothing in the corpus labels a sensor
   failure, a clock problem, or a QoS fault. Seven of eight detectors have no real-world
   ground truth at all — their 1.000/1.000 is synthetic-only.

Also note: the 12-flight → 105-flight episode is the standing warning. Any number measured
on a subset that something *selected* (`audit_corpus.py` ranks by interest) should be
assumed flattering until re-run on everything.

---

# Phase 1 — Make the numbers true

**Nothing else may start first.** Every downstream feature — alerting, on-vehicle
deployment, fleet analytics, the launch — inherits the detector's error rate. Building on
0.381 means building something that cries wolf twice for every real call, and the rework
is total.

### P1.1 — Split the false positives before fixing anything *(do this first; it is cheap)*
239 of 391 findings match no label. The eval counts **both** merged system-wide stalls and
`subsystem_failure` findings, but only the merged stall is the claim the tool actually
makes about the recorder. Split the 239 by class and by flight.

Possible outcome: the recorder-stall claim is much more precise than 0.381 and the noise
is all subsystem findings, in which case the fix is reporting, not detection — the same
shape as Tier 1.5. **Do not skip this. It may redefine the whole phase.**
*Depends on: nothing. Half a day.*

### P1.2 — Fix the precision the split identifies
The false positives concentrate on **short** flights with zero recorded dropouts:
`d4c32e25` (1432s, 0 labelled, 18 reported), `cbbf1568` (991s, 0, 17), `7b18658c`
(249s, 0, 16), `a2fe7c84` (156s, 0, 12), `cb59d4eb` (103s, 0, 11). Tier 1.6 handled topics
that had *not started yet*; this is flights where every topic is present and simply idle.
Investigate before fixing — the previous two fixes both succeeded because the cause was
measured first, and the one guess made this session (about the duration bug) was wrong.

**Recall is 1.000 and must stay there.** A precision fix that costs recall is a worse tool;
gate on it.
*Depends on: P1.1.*

### P1.3 — Get real ROS 2 recordings ⚠️ the biggest gap
Sources, in order of effort: public rosbag datasets (TUM, KITTI-format conversions,
Autoware samples); ROS Discourse "here's a weird bag" threads; any hardware you can borrow
for twenty minutes. Target: **20+ real recordings across at least 3 distinct robot types**,
audited and, where possible, labelled.

Until this exists, the README's four-format claim is one format validated and three
asserted.
*Depends on: nothing. Can run in parallel with P1.1/P1.2.*

### P1.4 — A small `.ulg` fixture for CI
The format with the most real coverage has the least automated protection, because a real
flight is 70 MB. Truncate one to a few seconds and a handful of topics, or synthesise a
minimal valid ULog. Then put `.ulg` in the format-equivalence test with the other three.
*Depends on: nothing. Small.*

### P1.5 — Re-baseline everything and re-record the demo
After P1.2, regenerate `REAL_DATA.md` over all 105 flights, update README and ROADMAP, and
re-run `scripts/record_demo.sh`. **The demo GIF encodes live numbers and goes stale
silently** — it already did once this session when the topic count moved 115 → 113.
*Depends on: P1.2, P1.3.*

**Phase 1 is done when:** precision on the full corpus is defensible or explicitly
argued as the right trade; recall is still 1.000; at least one real non-PX4 corpus is
scored; `.ulg` is in CI; every published number was regenerated after the last code change.

---

# Phase 2 — Make it run on the vehicle

**Depends on Phase 1** because an alarm that is wrong two times out of three gets muted
after the third false alert, and then the on-vehicle work is dead weight. The live
*mechanism* is already proven; what Phase 2 adds is the wire, the alerting semantics, and
the resource proof.

### P2.1 — ROS 2 `rclpy` source
The only genuinely missing piece for on-vehicle. Subscribe and feed `Auditor.push()`; the
detectors need no changes (proven by `test_live.py`). Pass
`get_topic_names_and_types()` as `expected_topics` so the correlation denominator floor
works live exactly as it does on a file.
*Depends on: Phase 1.*

### P2.2 — Alert semantics
**The raw verdict flaps.** Observed on a real flight at 40×: usable → compromised → usable
→ compromised → usable, crossing the threshold four times in nineteen seconds. Do not
alert on the verdict, and do not alert on "a new finding appeared" — findings are
legitimately *revised* (`aperiodic` withdraws after warmup; `dropped` is corrected once a
stall is attributed to the recorder).

Alert on **stall coverage growth**, which is the one quantity tested to be monotonic
(`test_live_findings_are_revised_but_stall_coverage_never_shrinks`). Add hysteresis and a
dwell time on top.
*Depends on: P2.1.*

### P2.3 — Byte-offset resume in the reader layer
`TailFeed` currently re-reads the whole file on every growth poll and skips what it has
already yielded — O(n²), fine for a demo, wrong for an hour-long recording. Needs readers
to resume from an offset. Fixes a known defect in code written this session.
*Depends on: nothing technically; only worth doing once P2.1 makes tailing real.*

### P2.4 — Cheaper snapshots
A snapshot serialises and restores the entire auditor: ~14% overhead at 1 Hz on a 2.7 kHz
stream, and it will not hold at 10 Hz or on a much wider topic set. The round-trip is
deliberate (it keeps the live auditor untouched and exercises the checkpoint path), so
optimise rather than remove — an incremental or copy-on-write state would keep both
properties.
*Depends on: P2.1.*

### P2.5 — Prove the resource envelope
The claim is <2 KB of state per topic. Measure it on a real ROS 2 stack, on the hardware a
customer would actually use, with CPU and memory over a full mission. **Throughput is
~25k msg/s in Python** — fine for PX4's ~2.7 kHz, likely not fine for a camera-heavy ROS 2
stack. Find the real ceiling before someone else does.
*Depends on: P2.1, P1.3.*

### P2.6 — Landing-time ingestion
On touchdown: audit, then push to the catalog. No new detectors, and it is the hinge
between Phase 2 and Phase 3 — without it, Phase 3 has no data.
*Depends on: P2.1.*

**Phase 2 is done when:** a monitor runs a full mission on real hardware, inside a measured
resource envelope, and emits alerts an operator does not mute.

---

# Phase 3 — Make it a fleet product

**Depends on Phase 2** because every feature here needs many missions from *known
vehicles*, and P2.6 is what produces them. Built earlier, these run on a corpus too small
and too anonymous to say anything.

### P3.1 — Per-unit fingerprinting
"SN-0043's IMU noise floor has been climbing for six flights." The smallest real step from
auditing a file to tracking a vehicle, and it reuses `to_state()` directly.
*Depends on: P2.6.*

### P3.2 — Pre-flight gate
"Should this vehicle fly today?" — audit the last N recordings, block or warn. The feature
an operations team feels immediately, and the natural demo for Phase 3.
*Depends on: P3.1.*

### P3.3 — CI regression gate
`--gate` already exists in the eval harness. Wire it to a GitHub Action so a regression in
recording quality fails a PR. Different buyer (platform teams), same engine.
*Depends on: P1.4 (needs `.ulg` in CI to be meaningful).*

### P3.4 — Cross-mission comparison
"Has this happened before?" — the question the README already promises. Needs a corpus with
enough labelled history to answer honestly, which is why it is last.
*Depends on: P3.1.*

### P3.5 — Model-in-the-loop evals, then ship
`evals/model_loop.py` is built and tested against a scripted client; it needs credentials
(`ANTHROPIC_API_KEY`, or `ant auth login` — a Claude subscription does **not** provide API
access) and real spend. Then PyPI, the blog post, ROS Discourse, r/ROS, HN.
*Depends on: Phase 1 (do not publish numbers that are about to change).*

---

## Complete weakness register

Carried forward so none of these is rediscovered the hard way.

| # | Weakness | Evidence | Addressed in |
|---|---|---|---|
| W1 | Precision 0.381 on real flights — 2 of 3 stalls are false | `REAL_DATA.md`, 105 flights | P1.1, P1.2 |
| W2 | Verdict oscillates live; alerting would flap | 4 threshold crossings in 19s on `588ff157` | P2.2 |
| W3 | `TailFeed` is O(n²) | re-reads whole file per poll | P2.3 |
| W4 | Snapshot cost ~14% at 1 Hz | full serialise/restore each time | P2.4 |
| W5 | No ROS 2 source | `rclpy` unwritten | P2.1 |
| W6 | ~25k msg/s ceiling in Python | per-message overhead | P2.5 |
| W7 | `.ulg` absent from CI | no small fixture | P1.4 |
| W8 | Model evals unrun | no credentials | P3.5 |
| W9 | **No real non-PX4 data at all** | `find` returns nothing outside `tests/` | P1.3 |
| W10 | 7 of 8 detectors have no real-world ground truth | only dropouts are labelled | P1.3 |
| W11 | Numbers measured on selected subsets flatter | 0.832 (12 flights) → 0.381 (105) | standing rule |
| W12 | Findings are revised, not monotonic | `aperiodic`, `dropped` withdraw | documented; P2.2 |
| W13 | Demo GIF encodes live numbers, goes stale silently | 115 → 113 topics this session | P1.5 |

## Gaps competitors cannot close — protect these

1. **"Can I trust this?" before "what does it say?"** Foxglove will plot a stall as if it
   were data. Nobody else audits the recording itself.
2. **Agent-native, not GUI-native.** Token budgets, provenance on every number, `caveats`
   telling a model what it must *not* conclude. A GUI cannot go in an agent loop.
3. **Same code file or wire.** Verified: identical findings offline and live. A competitor
   with an offline analyser needs a second implementation and must keep the two agreeing.
4. **Published field metrics against labels written by someone else.** 0.832 became 0.381
   and was published anyway. This is the credibility asset — protect it above any number.

## Rules that survived contact this session

- **Detectors stay single-pass with bounded state.** This bought the entire live path for
  one 250-line file. Do not spend it.
- **Measure before fixing.** Two fixes worked because the cause was measured; the one
  guess (the duration bug's cause) was wrong in a way that would have produced a wrong fix.
- **Re-run the measurement on everything, not on a sample.**
- **Never hand-edit a generated file.** Regenerate it or leave it stale and say so.
