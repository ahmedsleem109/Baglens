# Phase 3 — the spec

**Read this before implementing anything.** `ROADMAP.md` is the history and the reasoning;
this file is what to do next and in what order. `ENHANCEMENTS.md` is explicitly out of
scope and should not be used to plan work.

The ordering is by **dependency, not by appeal**. Each phase is unsafe to start before the
one above it is done, and the reason is given in each case.

---

# START HERE — the moat plan

> ## ⚠️ This file is finished. The current spec is [`NEWFEATURES.md`](NEWFEATURES.md).
>
> M1–M4 landed 2026-08-16 and M5 shipped: the repo is public, described, and every
> published number regenerated. What remains of M5 needs a human — a PyPI trusted
> publisher, a `v0.3.0` tag, and the announcement.
>
> **Do not plan work from this file.** It is now a record of what was done and why, kept
> for its weakness register and for the negative results at the bottom, which are the part
> most likely to be re-derived the hard way. Build order for what comes next — data age,
> pre-flight, TF integrity, node attribution — is in `NEWFEATURES.md`.

**M1–M4 are done (2026-08-16). M5 shipped except for the steps that need a human.**

The goal they add up to: **the only robot-recording tool that publishes field-measured
precision and recall for every detector, and that refuses to answer when it cannot.**
Being an MCP server is no longer a differentiator (there are at least three others), and
the fleet catalog now overlaps Foxglove's Data Search, shipped April 2026. Measured
honesty is the part nobody else has.

### M1 — Fault injection into real recordings ✅ done
`tests/synth/inject.py` copies a real recording and removes, thins, stretches or shifts a
known window of it, keeping the source's own jitter, burstiness, topic mix and QoS.
`evals/integrity/injected.py` scores all eight detectors against those labels:
**recall 0.824, precision 1.000 over 34 exact labels on 5 real recordings** across 4
platforms (`INJECTED.md`). Closes **W10**.

Three things learned building it, all recorded in the module and the eval:

* **Scoring must be differential.** A real recording has findings of its own; each base is
  audited clean and its findings become a baseline, so only findings that appear *with*
  the fault are attributed to it. Without that, the number measures the recording.
* **A label that removed nothing is worse than no label.** The first `nuway_stops`
  variants injected dropouts onto a topic that does not publish in the copied window, and
  handed every detector a free miss. Faults now record how many messages they moved, and a
  void label is refused rather than scored.
* **A label the detector cannot satisfy measures the recording's length, not the
  detector.** The first run scored 0.615/0.800 because it wrote `rate_degradation` labels
  on recordings shorter than D3's 80-second minimum history and `recorder_lag` labels
  below D6's 100 ms floor. Both numbers are published in `INJECTED.md`.

### M2 — W15 settled with the labels M1 produced ✅ done, and the old answer was wrong
The register recorded that making D7 honour `unassessable` cost 22+ points of PX4 recall.
**It does not reproduce.** That measurement was taken while the interval cap still ranked
by duration — the bug that independently cost 35 of 152 labels. Re-measured on all 105
flights *and* the injected ROS 2 labels (`scripts/w15_rules.py`, `W15_RULES.md`):

| D7 rule | PX4 R/P | injected R/P | `nuway_stops` |
|---|---|---|---|
| unrestricted (was shipped) | 0.993 / 0.942 | 0.824 / 1.000 | compromised at 0.0, 627s "stall" |
| aperiodic may not create | 0.993 / 0.942 | 0.824 / 1.000 | 7s |
| **may not create or vote (ships)** | **0.993 / 0.955** | 0.824 / 1.000 | 8s |

Zero recall cost, 1.3 points of precision gained, phantom stall gone. Unmatched findings
on the PX4 corpus fell **239 → 7**. The rule is now applied detector-wide, which is what
"a detector-wide rule must be applied detector-wide" was always supposed to mean.

### M3 — It refuses ✅ done
`src/baglens/detectors/assessability.py`. Four independent floors — assessable topic
share, assessable message share, coverage of the wall clock, duration against cadence
warmup — each reported when it is the one that failed. `verdict="unassessable"` overrides
the score, and `assessability.reasons` says why. Propagated through the MCP surface, the
caveats, `quality_gate.py` and the training gate.

**M2 alone would have been a new wrong answer.** With the D7 fix and without refusal,
`nuway_stops` reads *trustworthy at 98.7* — a parked bus where 0 of 70 topics have a
measurable rate, now claiming health because the false alarm was removed and nothing
replaced it. The two fixes only work together:

| recording | before M2 | M2 alone | M2 + M3 |
|---|---|---|---|
| `nuway_stops` | compromised at 0.0 | trustworthy at 98.7 | **unassessable**, 3 reasons |
| `nuway_waypoints` | trustworthy | trustworthy | trustworthy, confidence 1.0 |
| `tesla3_av` | usable_with_caveats | usable_with_caveats | usable_with_caveats, conf 1.0 |
| `demo.mcap` (3 msgs) | trustworthy | trustworthy | **unassessable** |

### M4 — The training-data gate ✅ done
`baglens gate <dir>` (`src/baglens/gate.py`): accept / review / reject per episode, a
reason code and a human reason for every rejection, and a `train_on` list a training job
consumes directly. The manifest records the policy it was produced under, so a stricter
run is distinguishable from a worse dataset.

**The LeRobot question is answered, and the answer is no.** LeRobot datasets recompute
per-frame timestamps as `frame_index / fps` during conversion — measured across
`lerobot/pusht` (10 fps) and `lerobot/aloha_static_coffee` (50 fps), inter-frame deltas
vary only by float rounding (~1e-6 s). Every trace of recorder timing is gone, so D1–D7
have nothing to find there; a capture-side dropout survives as frames that do not exist,
with the timestamps closed seamlessly over the hole. **It is not "a small reader".**
Auditing that format needs a different detector family based on kinematic discontinuity —
a joint-state jump between two frames the fps claims are 20 ms apart. That is a separate
project, not a reader, and the gate says so in its own docstring.

### M5 — Open-source release ⬅ *the remaining work*
Repo is still **private**. Written and green: `CHANGELOG.md`, `SECURITY.md`, issue
templates, and `.github/workflows/release.yml` (trusted publishing, and it installs the
built wheel into a clean environment and imports it before publishing). Still to do: flip
the repo public, set the GitHub description and topics, tag `v0.3.0`, and re-record
`docs/assets/demo.gif` — it encodes live numbers and this session moved all of them.

## Rules that must survive into the next session

1. **Re-run the labelled corpus after any detector change, including performance-only
   ones.** `uv run python scripts/split_false_positives.py --dir ~/data/public/px4`
   (~6 min). A bounded-state cap added purely for memory cost 35 of 152 labelled dropouts
   while `pytest` and both synthetic evals stayed green.
2. **Check disk before downloading.** `df -h /mnt/d`. The WSL ext4 image lives on the
   Windows D: drive; `df /` inside WSL reports the virtual size and is meaningless. Filling
   the host drive once remounted ext4 read-only mid-write and the distro would not boot.
3. **Never tune against unlabelled data.** `nuway_stops` has no labels. Neither does any
   ROS 2 recording here until M1 gives them some.
4. **Never hand-edit a generated file.** Regenerate it or leave it stale and say so.

---

## State as of 2026-08-15

**Green:** 211 tests + 2 skipped, `ruff`, `mypy`. 43 MCP tools, 8 detectors, 4 formats —
all four now in the format-equivalence test.

**Measured, real:** recall **0.993**, precision **0.942** for `correlation` against PX4's
own dropout records across **105 distinct flights** (677 minutes, 152 labelled dropouts).
`evals/integrity/REAL_DATA.md`. The single missed dropout is the price of the bounded
interval store, not a detection failure.

**Measured, synthetic:** 1.000 precision/recall across 8 fault classes, 0.000 false
positives on 40 clean bags — and the same under `BAGLENS_EDGE_PROFILE=1`, so the device
profile costs no detection.

**Real ROS 2 data exists now:** 3 recordings, 2 platforms, `evals/integrity/ROS2_DATA.md`.
Unlabelled, so no precision or recall is claimed from it — but it found a real defect
within an hour of arriving.

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

### P1.3 — Get real ROS 2 recordings ⚠️ *corpus exists; labels still missing*

**Done:** 11 real recordings across **5 platforms**, 64 minutes, on disk at
`~/data/public/ros2` and audited in `evals/integrity/ROS2_DATA.md`. Ten are *natively
recorded* — written by `ros2 bag record` on the robot itself, which is the only kind that
can support a claim about recorder behaviour:

| Platform | Recordings | Source | Format |
|---|---|---|---|
| Autonomous shuttle bus (Nav2) | 2 | `xrkong/nuway_rosbag` | mcap |
| Road vehicle (Tesla Model 3) | 1 | `tfoldi/tesla3_av_rosbags` | mcap |
| Quadruped / IMU rig | 1 | `UniflexAI/rosbag2_imu_example` | **db3** |
| Short-run rig | 6 | `Dongkkka/rosbag_test` | mcap |
| Handheld LIVO rig *(converted)* | 1 | `DapengFeng/MCAP` | mcap |

`scripts/fetch_ros2.sh` fetches them and now refuses a download that would leave the host
disk short. The `.db3` is the first real rosbag2 SQLite this project has ever opened —
every previous `.db3` test ran on a file it converted itself.

**It has paid for itself twice.** `nuway_stops` exposed W15 (a phantom 1,489-second
stall). The Tesla exposed a reporting defect nothing synthetic could have: two overlapping
`rate_degradation` findings for one drift, one of which read *"sped up by 65%
(1715.1 → 1650.3 Hz)"* — a sentence contradicting its own numbers, because the direction
came from the episode's peak slope while the rates were read from a bucket ring that had
since moved on. Now one finding, derived from the two rates it prints.

**Still open:** the target was 20+ recordings across 3+ robot types — 5 platforms is there,
the count is not. And there are still **no labels outside PX4**, so W10 stands: nothing
here supports a precision or recall claim, and `ROS2_DATA.md` says so on its first line.
Closing that is fault injection into these recordings, not more downloading.

⚠️ **Disk discipline is not optional.** Fetching this corpus filled the host disk, the WSL
ext4 image remounted read-only mid-write, and the VM would not boot until space was freed.
Check free space before fetching, and keep the local corpus under a few GB.

Remaining sources, in order of effort: more Hugging Face `rosbag2`/`mcap` datasets (the
`DapengFeng/MCAP` collection alone holds 41, mostly too large for this disk); public rosbag
datasets (TUM, KITTI-format conversions, Autoware samples); ROS Discourse "here's a weird
bag" threads; any hardware you can borrow for twenty minutes. Target remains **20+ real
recordings across at least 3 distinct robot types**, audited and, where possible, labelled.
*Depends on: nothing.*

### P1.4 — A small `.ulg` fixture for CI ✅ done
`tests/synth/generate.to_ulg` writes the clean fixture's own schedule as a real ULog —
a few tens of KB, generated not committed, with the logger's dropout records included so
the ground-truth path the real-data eval depends on is exercised too. `.ulg` is now the
fourth format in `test_all_formats_reach_the_same_conclusions`.

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

| # | Weakness | Evidence | Status |
|---|---|---|---|
| W1 | Precision 0.381 on real flights — 2 of 3 stalls are false | `REAL_DATA.md`, 105 flights | **closed** — 0.942 at recall 0.993, see `FP_SPLIT.md` |
| W2 | Verdict oscillates live; alerting would flap | 4 threshold crossings in 19s on `588ff157` | **closed** — `alerts.py` fires on stall-coverage growth |
| W3 | `TailFeed` is O(n²) | re-reads whole file per poll | **closed** — `ScanCursor`, one pass total |
| W4 | Snapshot cost ~14% at 1 Hz | full serialise/restore each time | **the 14% was wrong: +131%.** Halved to +116%; see below |
| W5 | No ROS 2 source | `rclpy` unwritten | **closed** — `ros2.py`, tested against a stub graph |
| W6 | ~25k msg/s ceiling in Python | per-message overhead | **measured: 44.5k msg/s** on a real PX4 flight |
| W7 | `.ulg` absent from CI | no small fixture | **closed** — `to_ulg`, in the four-format test |
| W8 | Model evals unrun | no credentials | **open** — still no `ANTHROPIC_API_KEY` |
| W9 | **No real non-PX4 data at all** | `find` returns nothing outside `tests/` | **closed** — 11 recordings, 5 platforms, mcap + db3 |
| W10 | 7 of 8 detectors have no real-world ground truth | only dropouts are labelled | **closed** — 34 injected labels on real recordings, all eight scored: 0.824/1.000 (`INJECTED.md`) |
| W18 | `rate_degradation` summaries could contradict their own numbers | Tesla CAN bus: "sped up by 65% (1715.1 → 1650.3 Hz)", twice for one drift | **closed** — one finding per topic, sentence derived from the printed rates |
| W11 | Numbers measured on selected subsets flatter | 0.832 (12 flights) → 0.381 (105) | standing rule |
| W12 | Findings are revised, not monotonic | `aperiodic`, `dropped` withdraw | documented; `alerts.py` builds only on the monotonic quantity |
| W14 | `<2 KB/topic` was false by 3.6× | 7,360 B on a 118-topic flight | **closed** — edge profile is 2,016 B and gated in CI |
| W15 | D7 over-reports on event-driven-heavy recordings | `nuway_stops`: 477s of "stall" on a stationary bus | **closed** — D7 honours `unassessable`; 0 recall cost, +1.3 precision (`W15_RULES.md`) |
| W19 | A recording nothing can be measured in reads as healthy | `nuway_stops` at *trustworthy 98.7* once W15 was fixed | **closed** — `unassessable` verdict, M3 |
| W13 | Demo GIF encodes live numbers, goes stale silently | 115 → 105 topics this session | **closed** — re-recorded; also fixed the 96-column wrap that made the timeline unreadable |
| W16 | `CorrelationDetector.results` was unbounded | no cap, unlike every other accumulator | **closed** — `max_results=1000`, ranked by concurrency, costs 1 of 152 labels |
| W17 | The ULog reader is not streaming | `pyulog` loads a 66 MB flight into ~250 MB of numpy | **open** — the detectors are bounded; this reader is not |

**On W15 — a negative result that was itself wrong, which is the more useful lesson.**

The first real ROS 2 recording produced a 1,489-second "system-wide stall" on a
1,492-second file: a stationary shuttle bus, 70 of whose 110 topics are event-driven. The
obvious fix — make D7 honour `unassessable`, as D2 and the per-topic scores already do —
was tried four ways, appeared to cost 22+ points of PX4 recall every time, and was
therefore refused. That refusal was recorded here, in the detector's docstring, in the
auditor's, and in a memory, in enough detail to look settled.

It was wrong. Re-measured on all 105 flights and on the injected ROS 2 labels
(`scripts/w15_rules.py` → `W15_RULES.md`):

| D7 rule | PX4 R/P | injected R/P | `nuway_stops` |
|---|---|---|---|
| unrestricted (was shipped) | 0.993 / 0.942 | 0.824 / 1.000 | compromised at 0.0, 627s "stall" |
| aperiodic may not create | 0.993 / 0.942 | 0.824 / 1.000 | 7s |
| **may not create or vote (ships)** | **0.993 / 0.955** | 0.824 / 1.000 | 8s |

No recall cost, precision up, phantom stall gone, unmatched PX4 findings 239 → 7.

**Why the old numbers said otherwise:** they were measured while the correlation interval
cap still ranked by duration — the bug that independently evicted 35 of 152 labelled
dropouts. The restriction was charged for damage another bug had done, and the parenthetical
noting that possibility was written and then not acted on.

> **A measurement taken while another bug is live can convict the wrong change.** When a
> fix is rejected on evidence, put it back on the list to re-run after the next bug is
> found. This one sat rejected for two sessions on a number that was never true.

**And fixing it, alone, made the recording worse.** With D7 corrected and nothing else,
`nuway_stops` reads *trustworthy at 98.7*: a parked bus where 0 of 70 topics have a
measurable rate, now claiming health, because removing a false finding leaves a short
finding list and a short finding list is indistinguishable from a clean recording. That is
W19, and M3's refusal path is what makes the detector fix safe to ship. Neither was
correct on its own.

`max_stall_fraction=0.5` stays as a backstop. It was never a solution — it bounded the
artefact rather than removing it — and it is cheap to keep.

**On W4.** The published 14% was not a regression, it was never measured: snapshots at 1 Hz
on a 2.7 kHz stream more than double the run. Serialisation was not the cost — `finish()`
was, and inside it a quadratic `min()` over every interval ever recorded. One snapshot is
now ~80 ms rather than ~162 ms. Going further means assembling reports incrementally, which
is a real redesign and should not be started without a reason better than tidiness.

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
- **Re-run the corpus after *any* detector change, including one made for performance.**
  The interval cap added under P2.4 was a bounded-state fix with no intended effect on
  findings. It ranked by duration, the longest silences in a recording belong to isolated
  slow topics, and it therefore evicted the short high-concurrency intervals that *are*
  the stalls — 35 of 152 labelled dropouts, invisible to the test suite and to the
  synthetic evals, both of which stayed green. Only the labelled corpus caught it.
- **A published number with no script behind it is a guess.** Both numbers that turned out
  to be wrong this session — `<2 KB per topic` and `~14% snapshot overhead` — were the two
  with no harness regenerating them. Every claim now has a script: `bench.py`,
  `bench_snapshot.py`, `split_false_positives.py`, `ros2_data.py`.
- **A detector-wide rule must be applied detector-wide.** `unassessable` was honoured by
  D2, by the per-topic scores and by `find_gaps`, and ignored by D7 — which is how one
  quiet shuttle-bus recording became a 1,489-second "system-wide stall". When a rule about
  what the tool refuses to judge is added, grep for every detector that should obey it.
