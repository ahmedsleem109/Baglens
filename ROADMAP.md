# Roadmap

Phases 0–7 of `PLAN.md` are built and green: 42 tools, eight detectors at 1.000
precision/recall on the synthetic matrix, 54 tests, 56 eval cases passing.

What follows is what is actually left, ordered by whether it blocks a claim the project
already makes. Tier 0 is correctness debt — code that ships today without ever having
been executed against the thing it claims to support. Nothing below Tier 0 should be
started before Tier 0 is done, because everything below it inherits the same risk.

---

## Tier 0 — Verification debt (blocks the launch)

### 0.1 Find a real problem in data nobody here made ⭐

**This is the Phase 2 definition of done, and it is the only item that converts the
precision/recall table from a regression gate into a credible claim.**

The current numbers come from a generator in this repository. Perfect scores mean the
detectors and the generator agree about what a fault looks like — which is partly
circular, and the README says so.

- Pull a few hundred public recordings: Foxglove samples, PX4 flight logs
  (review.px4.io), ROS 2 datasets on Hugging Face.
- `catalog.add_source`, then `compare.rank_missions(metric="health_score")`.
- Manually inspect the twenty worst. Confirm or dismiss each finding by hand.
- Both outcomes are publishable: a real bug is the blog post; everything clean is a
  genuinely low false-positive rate, and you go looking at messier sources (ROS
  Discourse "help, my bag is weird" threads are the goldmine).

**Done when:** one confirmed, previously-unreported integrity problem in a public
dataset, written up with the recorder-lag curve or the timeline that shows it.

### 0.2 Execute the formats the README claims

Three readers ship with zero real-world execution:

| Reader | State | Needed |
|---|---|---|
| `db3_reader.py` | Written, never opened a real `.db3` | A rosbag2 SQLite fixture in the generator; reader + auditor tests |
| `ros1_reader.py` | Written, never opened a real `.bag` | A ROS 1 fixture via `rosbags`; reader + auditor tests |
| `ulog_reader.py` | Written, `pyulog` not installed, never run | Install the extra, test against a downloaded PX4 `.ulg` |

The README says "`.mcap`, `.db3`, `.bag` and PX4 `.ulg` are all read in pure Python".
Today only `.mcap` is proven. Either prove the other three or narrow the claim.

**Done when:** the generator emits `.db3` and `.bag` fixtures, `tests/integration`
runs the full auditor over each format, and one real `.ulg` audits end to end.

### 0.3 Finish or delete the two half-wired features

- **Event-anchored alignment.** `compare.missions(align="event")` is advertised in the
  tool description and in `AlignMode`, but `MissionSignal.aligned()` is always called
  without an anchor, so `event` behaves identically to `absolute`. Either compute the
  anchor (first non-zero `/cmd_vel`, or a named log line) or remove the mode.
- **Continuation tokens.** `budget.apply_budget` mints a `continuation_token`, and no
  tool accepts one back — the round trip does not exist. Either add a
  `continuation_token` parameter to the paginating tools (`catalog.list_missions`,
  `health.find_gaps`, `logs.query`) or drop the field.

A field an agent cannot act on is worse than a missing one: it invites a wasted call.

**Done when:** both are either functional with a test, or gone.

### 0.4 Type checking honest

`mypy src/baglens` currently fails on a packaging/namespace error before it type-checks
anything, and CI runs it with `continue-on-error: true`. Fix the module resolution, work
through the real errors, then remove the escape hatch.

**Done when:** `mypy --strict src/baglens` is green and gating.

### 0.5 Transports and paths that were never exercised

- `--http` sets `mcp.settings.host/port` behind a `# type: ignore`; that attribute may
  not exist on the v2 `MCPServer`. Never started. Smoke-test both transports.
- Background indexing (`catalog.add_source(background=True)`) is only tested
  synchronously; the thread path and `index_status` progress reporting are unproven.
- Root confinement is tested at the `resolve()` level but not through a live tool call.

---

## Tier 1 — Completeness against the plan

### 1.1 The missing health tool

`PLAN.md` §2.4 lists `health.qos_report`. Today QoS mismatch surfaces only as a finding
inside the audit. Promote it: recorded QoS profiles per topic, reliability/durability
mismatches that cause silent drops, and the declared-vs-observed rate comparison the
cadence estimator already computes.

### 1.2 Fixtures for the code paths nothing reaches

Every one of these is written and untested because no fixture produces the input:

- **CRC mismatch** — `recovery.py` handles it, no fixture corrupts a chunk.
- **Growing / in-progress file** — `_is_growing()` is never tested against a file that
  is actually being written.
- **Raw `sensor_msgs/Image`** — only `CompressedImage` is exercised; the `rgb8`/`bgr8`
  unpacking path has never run.
- **`PointCloud2`** — `pointcloud.summary` has no fixture at all.
- **`/plan` topic** — `spatial.trajectory_deviation` has never compared two real paths.
- **`logs.correlate_with_signal`** — no test.

Add each fixture to `tests/synth/generate.py`, then a test per path. Cheap work, and it
is exactly where a silent regression would hide.

### 1.3 Golden-file snapshots

`PLAN.md` §9.2 calls for snapshot tests over tool outputs at fixed seeds, to catch
unintended behaviour drift. The eval suite checks assertions, not shape — a formatting
or field change slides through both today.

### 1.4 Field-level redaction

`BAGLENS_REDACT_TOPICS` masks whole topics. `BAGLENS_REDACT_FIELDS` is parsed from the
environment and then never used — GPS coordinates inside an otherwise-fine topic cannot
be masked. This matters for the defence, medical and compliance-restricted teams the
privacy posture is aimed at.

### 1.5 Health-score calibration against real data

`gap_penalty` is measured against 5% of the recording's length, the weights are
guesses, and the thresholds have only ever been checked against synthetic 120-second
bags. Re-tune once Tier 0.1 has real recordings, and publish the before/after.

---

## Tier 2 — Launch (the reason the project exists)

### 2.1 Model-in-the-loop evals ⭐

`evals/runner.py` scores reference tool sequences deterministically. The interesting
number — and the one that makes this legible to ML engineers rather than only
roboticists — is what an actual model does with the surface: correctness, tool-call
efficiency, token consumption, and hallucination rate across several models.

The harness, the scoring axes and the cases already exist; what is missing is the
`--model` path that hands the question and the live tool surface to an LLM and scores
the trajectory. Publish `evals/RESULTS.md` across models.

### 2.2 The README demo

A 30-second asciinema → GIF of an agent finding a real bug in a real bag. One sentence,
then the GIF. This is the single highest-leverage hour in the whole project and it
cannot be written before 0.1 lands.

### 2.3 Ship it

- PyPI release so the one-liner is `uvx baglens` rather than `uvx --from git+…`.
- Blog post: *"Your rosbag is lying to you: detecting silent data loss in robot
  recordings."* Open with the real finding, not the feature list.
- ROS Discourse (proven audience — the previous rosbag MCP servers were announced
  there), r/ROS, Hacker News, LinkedIn.
- Then engage for a week: reply to every comment, fix reported bugs within 48 hours. The
  thread is the artifact you point at later.
- `good first issue`s from Tier 1.2 — they are genuinely well-scoped for a newcomer.

---

## Tier 3 — The edge device, which is why the detectors are streaming

Everything above is the offline product. This is the direction the whole architecture
was bent for, and none of it should start before the offline version has users.

### 3.1 Live source

A `LiveSource` feeding the same `Auditor` from a live subscription instead of a file —
the detectors need no change, which is the entire payoff of the streaming constraint.
Start with an MCAP tail (a file being written) before touching rclpy, because it needs
no ROS installation and proves the same thing.

### 3.2 Checkpoint and restore

Detector state is described as "a fixed-size struct, serialisable, so it can be
checkpointed" — the sizes are asserted, the serialisation does not exist. Implement
`to_state()` / `from_state()` per detector plus a round-trip test that splits a
recording in half and proves identical findings.

### 3.3 Live-tail catalog

Watch a directory for split recordings and index closed splits as they land, giving
near-live corpus visibility without touching the writer. Linux-filesystem only —
`inotify` is unreliable across `/mnt/c`.

### 3.4 Only if profiling demands it

Rust indexer via PyO3; S3/GCS remote reads over the MCAP index with byte-range requests;
a CI regression-gate GitHub Action; `compare.explain` writing full incident reports.

The current bottleneck is per-message Python overhead at ~25k msg/s. Measure a real
workload before rewriting anything — the catalog already means bags are read once.

---

## Deliberately not doing

Restated from `PLAN.md` §0.4, because scope creep here is the main risk:

- Not a visualiser. Foxglove is better; hand off to it.
- Not a live robot controller. Read-only, offline, forensic.
- Not a cloud platform. Local-first, zero telemetry.
- Not a storage format. MCAP won.
