# Session handoff — 2026-08-16

State at the end of the session that built F1, F2 and F3. **Read `NEWFEATURES.md` and
`CLAUDE.md` first; this file only records where things actually stand.**

---

## Branches and PRs — none merged, all open for review

| Branch | PR | Base | State |
|---|---|---|---|
| `f1-data-age` | [#1](https://github.com/ahmedsleem109/Baglens/pull/1) | `main` | pushed, green |
| `f2-preflight` | [#2](https://github.com/ahmedsleem109/Baglens/pull/2) | `f1-data-age` | pushed, green |
| `f3-transforms` | [#3](https://github.com/ahmedsleem109/Baglens/pull/3) | `f2-preflight` | pushed, **plus one uncommitted fix — see below** |

Stacked deliberately: F2 checks F1's data-age budget, F3 fills the `tf` check F2 reports
as `unchecked`. Review in order.

## THE FIRST THING TO DO NEXT SESSION

There is a **fix in the working tree on `f3-transforms` that is not committed**:
`src/baglens/readers/db3_reader.py`. Commit it (tests were running when the session
ended — re-run `uv run pytest -q` first and confirm green).

### What the fix is, and why it matters more than it looks

**F1 and F3 did nothing at all on `.db3` recordings — the rosbag2 default format — and
reported clean, empty results while doing it.** Two silent failures stacked:

1. `Db3Reader` never *declared* `want_stamps`. The auditor opts a reader in with
   `if hasattr(reader, "want_stamps")`, and the reader only ever read the flag via
   `getattr(..., False)`. So it was never switched on.
2. `Db3Reader.schema_text()` returns a repr of the rosbags typestore AST, not
   message-definition text, so `stamp_offset()` — which parses msgdef text — returned
   `None` for every topic.

Neither raised. Both features looked like they worked. This was invisible because every
fixture and every real recording in the corpus was MCAP.

The fix reads offsets from the typestore's field list (`_stamp_offset_for`), declares the
flags as real attributes, and adds `decode_topics` support to the db3 arrival path.

### The same bug almost certainly exists in two more readers

**Check `ros1_reader.py` and `ulog_reader.py`.** Neither declares `want_stamps` or
`decode_topics`, so F1 and F3 are presumably no-ops on `.bag` and `.ulg` too. ULog has no
`header.stamp` concept and should legitimately report unmeasurable — but it should do so
*explicitly*, not by accident.

**Write the test that would have caught this**: a cross-format test asserting that every
reader either supplies stamps or explicitly declares it cannot. The existing
"all four formats reach identical conclusions" test did not catch it because it compares
detectors that were all silent in the same way.

After fixing: re-run both labelled corpora (rule 1) and re-run `evals/age/data_age.py`.
The published F1 numbers were measured on MCAP only and are unaffected, but say so.

---

## What each feature actually achieved

### F1 — data age (PR #1)

- Stamp peek verified against a full decode: **134 topics, 11 recordings, 0 disagreements**
  (`scripts/verify_stamp_peek.py`).
- Precision/recall **1.000/1.000 synthetic, 0.900/0.750 real**. Every injected fault at 2×
  the target topic's own noise band or above was caught (9/9); all three misses are at 1×,
  where the fault is the same size as the variance it hides in.
- Cost **+10.6%** on an arrival scan, **+58.6%** on a full audit
  (`scripts/bench_stamp_peek.py`). On by default; that is a live decision, see PR #1.
- Per-topic state 1,040 B. A log histogram would be faster but costs 2,048 B/topic —
  the whole budget — so P² was kept.

**Negative result worth more than the feature:** real robots mostly do not propagate
`header.stamp` through a pipeline. Across all 11 public recordings, 1–21% of stamps are
shared between topics, but almost all of that is sensor synchronisation (stereo pairs,
hardware-synced lidar) or driver-internal derivation. Exactly one genuine cross-node edge
exists in the corpus (`/imu/data → /odometry/global`), and **no recording contains a
perception → planning → actuation chain** — a full Nav2 shuttle bus restamps before
`/cmd_vel`. The per-stage feature is verified but corpus-limited.

### F2 — pre-flight gate (PR #2)

- **Zero false alarms across ten healthy synthetic graphs** — the number that decides
  whether anyone leaves it switched on.
- Catches missing topic, halved rate, a topic silent 20 s of 30, clock skew, and an
  already-degrading rate; names the topic in each. Verdict well inside the 35 s budget.
- Three statuses, not two: `unchecked` is listed and never counted as a pass.
  **Open question for the author: `unchecked` is non-fatal by default (`--strict` inverts
  it). Which should be the field default is an operations decision, not a code one.**

### F3 — transform integrity (PR #3)

- Healthy tree: 0 findings across 5 seeds. All four faults caught and named.
- TF decode cost **+11.4%** on a real recording.
- `baglens frames <rec> [--out tree.pdf|svg|dot] [--json]` — a `view_frames` that reports
  the diagnosis instead of a picture. The PDF is written by hand: no graphviz, no cairo,
  ~2 KB/page, validated in tests with `pypdf`.
- Two false-positive classes found only on real data: a recording with no `/tf` reported
  every frame as orphaned; one TF outage became 29 findings. Both guarded.

**Known limit:** duplicate-publisher detection reports *that* two sources disagree and by
how much, but not *which two nodes* — a bag carries no publisher identity. That is F4's
problem.

---

## Data

### What is on disk

| Path | Contents |
|---|---|
| `~/data/public/ros2` | 11 MCAP recordings, ~9.9 GB. Only `nuway_stops` has `/tf`. |
| `~/data/public/px4` | ~105 ULog flights, 152 real dropout labels |
| `~/data/injected` | 39 copies with injected faults, the `INJECTED.md` corpus |
| `~/data/public/autoware` | **new** — Leo Drive ISUZU `all-sensors-bag4`, `.db3`, 1.06 GB |

A guarded fetch (`~/tmp/fetch_autoware.sh`, log `~/tmp/fetch.log`) was running when the
session ended, pulling the `driving_*` bags. **It stops when free space on `/mnt/d` would
drop below 5 GB** — the floor the author set. Check `df -h /mnt/d` before doing anything
else; the WSL ext4 image lives on D: and filling it once left the distro unbootable.

### The Autoware bag has real faults, and they are severe

Published as a normal sample dataset, audited as `unassessable` with 23 findings:

| | |
|---|---|
| `/lucid_vision/camera_0/raw_image` | missing **34.9%** of frames (CRITICAL) |
| `/lucid_vision/camera_1/raw_image` | missing **34.1%** |
| `/lucid_vision/camera_2/raw_image` | missing **33.2%** |
| all three `camera_info` | missing 25–28% |
| lidar packets | 6–8% |

Structural faults F3 found once db3 was fixed: **two disconnected roots** (`base_link` and
`ned` — the GNSS frame is an island), and `/gnss/fix` stamping from a different clock.
15 TF edges, a real sensor-kit calibration tree, no false orphans.

### The honest problem with the corpus

Measured, four recordings, everything enabled:

| Recording | Verdict | Score | Findings |
|---|---|---:|---|
| `dongkkka_00` | trustworthy | 99.9 | 4 (stamp hygiene only) |
| `fastlivo_hku2` | trustworthy | 100.0 | 1 |
| `nuway_waypoints` | trustworthy | 95.0 | 2 |
| `tesla3_av` | usable_with_caveats | 84.2 | 6 |

The ROS 2 corpus is mostly healthy. The eight original detectors do have real fault data
(105 PX4 flights, 152 labels). **F1, F2 and F3 do not** — their evidence is fixtures plus
injection.

This is structural, not laziness: **healthy bags get published, broken ones don't.** The
Autoware find was an accident — a broken bag published as a good one. The author has
confirmed they have no in-house recordings to contribute.

`~/data/px4_fetch.log` records **447,141 public PX4 flights available** and a fetch script
that pulled 105. That is the deepest real-fault pool reachable, though ULog exercises none
of F1/F2/F3.

---

## Open questions for the author

1. **F2's `unchecked` default** — non-fatal, or `--strict` by default in the field?
2. **F1 on by default at +58.6%** — a real regression to a published throughput property.
3. **F3's `/tf_static` declared-but-empty heuristic** — currently downgraded to MEDIUM and
   attributed to the *recording* rather than the robot. Sanity-check that call.

## Recommended order for the next session

1. Commit the db3 fix; confirm the suite is green.
2. Fix the same class of bug in `ros1_reader` and `ulog_reader`; add the cross-format
   stamp/decode test that would have caught it.
3. Re-run both labelled corpora and the F1 eval; update numbers if they move.
4. Audit the newly fetched Autoware driving bags — the most likely source of real faults
   currently reachable.
5. **`baglens verify --expect findings.json <new.mcap>`** — this was recommended over F4.
   An agent can currently diagnose but has no way to ask "is this specific finding gone
   now?", so it cannot tell whether its fix worked and cannot iterate. F2's baseline is
   already this shape. It is the smallest change that turns baglens from something an
   agent reads into something an agent can work against.
6. F4 (node attribution) — the spec calls it the most speculative. Settle the node→topic
   mapping (live graph API / F2 baseline / `/rosout`) **before** writing code.
