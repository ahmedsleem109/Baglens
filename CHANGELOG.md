# Changelog

Notable changes to baglens. Numbers in this file are measured, and the script that
regenerates each one is named beside it — a published number with no script behind it is
a guess, and this project has shipped two of those before.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **End-to-end data age (F1)** — `src/baglens/detectors/age.py`, `health.data_age`.
  Follows `header.stamp` to report how old the data behind each topic was: P50/P95/P99 per
  topic, per stage where a chain exists, and a trend on the P99 tail. Single-pass,
  **1,040 B of state per topic**, checkpointable like every other detector.
  Precision/recall **1.000 / 1.000 synthetic, 0.900 / 0.750 on faults injected into real
  recordings** (`evals/age/data_age.py` → `evals/age/DATA_AGE.md`). Every fault at 2× the
  target topic's own noise band or above was caught, 9 of 9; the three misses are all at
  1×, where the fault is the same size as the variance it hides in.
- **Payload reading without deserialization** — `src/baglens/readers/stamp_peek.py`.
  `header.stamp` is read as an 8-byte `struct.unpack` at CDR offset 4, gated on the
  *schema* rather than on the bytes, so a `std_msgs/Float32` is never mistaken for a
  stamped message. Verified against a full decode on **134 topics across 11 real ROS 2
  recordings, zero disagreements** (`scripts/verify_stamp_peek.py`, exits non-zero on any
  disagreement). Available identically on the live ROS 2 path, which already subscribes
  raw.
- **`P2Quantile`** in `detectors/base.py` — streaming quantiles from five markers, within
  0.3% of the true P95 on 20k samples. Chosen over a log histogram because 256 bins would
  cost 2,048 B per topic, the entire per-topic budget, for one estimator.
- **Stale-pipeline injection into real recordings** — `tests/synth/inject.py` can now age
  one topic's stamps by a growing amount, editing eight bytes per message and leaving
  arrival times, sizes and topic mix untouched.

### Changed

- **The audit is no longer strictly payload-free when `data_age` is enabled** (it is, by
  default). Measured with `scripts/bench_stamp_peek.py` on a 200k-message recording: the
  peek costs **+10.6%** on an arrival scan, and the full detector **+58.6%** on an audit.
  Disable with `--detectors …` omitting `data_age` to get the old numbers back. ULog is
  unaffected — its reader offers no stamps.

### Notes

- **Negative result: real robots mostly do not propagate `header.stamp` through a
  pipeline.** Measured across all 11 public ROS 2 recordings, 1–21% of stamps are shared
  between topics, but almost all of that is sensor synchronisation (stereo pairs,
  hardware-synced lidar) or driver-internal derivation. Exactly one genuine cross-node
  edge exists in the corpus, and **no recording contains a perception → planning →
  actuation chain** — a full Nav2 shuttle bus restamps before `/cmd_vel`. The per-stage
  feature is therefore verified but corpus-limited; on such a robot F1 reports per-topic
  age and names the node that broke the trace.
- **A P99 from four samples is not a statistic.** An early version produced 16 false
  "data age is growing" findings on `nuway_stops`, the parked shuttle bus, including a
  claimed 57× rise on a 0.4 Hz topic. Buckets now require 100 age samples; sparse topics
  report `trend_assessable: false` rather than a verdict. This is W15's rule applied to a
  new detector, and it took the false positives from 16 to 3 without moving a threshold.

## [0.3.0] — 2026-08-16

### Added

- **Fault injection into real recordings** (`tests/synth/inject.py`). Copies a real
  `ros2 bag record` MCAP and removes, thins, stretches or shifts a known window of it,
  keeping the source's own jitter, burstiness, topic mix and QoS metadata. Emits a ground
  truth sidecar in the same schema `tests/synth/generate.py` uses, so one scorer reads
  both corpora.
- **Precision and recall for all eight detectors on real recordings**
  (`evals/integrity/injected.py` → `evals/integrity/INJECTED.md`): **recall 0.824,
  precision 1.000 across 34 exact labels on 5 real recordings** from 4 platforms. Scoring
  is differential — each base is also audited clean, and findings the clean copy already
  had are attributed to the recording rather than to the injected fault. This closes W10,
  the largest remaining credibility gap: every previous per-detector number was measured
  on a background this repository generated.
- **`baglens gate <dir>`** — a training-data gate (`src/baglens/gate.py`). Per-episode
  accept/review/reject with a machine-readable manifest, a reason code and a human reason
  for every rejection, and a `train_on` list a training job can consume directly. Reasons
  are the product: "3.2% of this episode fell inside a recorder stall" is actionable in a
  way that "score 61" is not.
- **`unassessable` verdicts** (`src/baglens/detectors/assessability.py`). A recording that
  cannot support a verdict now gets one that says so, with reasons, instead of a score.
  Four independent floors — assessable topic share, assessable message share, coverage of
  the wall clock, and duration against cadence warmup — each reported when it is the one
  that failed.
- `SECURITY.md`, issue templates, and a PyPI release workflow that installs the built
  wheel into a clean environment and imports it before publishing.

### Changed

- `CorrelationConfig.aperiodic_may_create` / `aperiodic_may_vote` make the W15 question
  re-runnable rather than remembered. `scripts/w15_rules.py` scores every rule against
  both labelled corpora and the recording that motivated the question.

### Fixed

- The injector dropped the final message of every copy: `iter_messages` treats `end_time`
  as exclusive. Caught by a test that counted messages rather than trusting the summary.

## [0.2.0]

### Added

- Live monitoring (`src/baglens/live.py`, `src/baglens/alerts.py`): a `ScanCursor` tail
  feed, checkpointable auditor state, and alerts that fire on stall-coverage growth — the
  one quantity tested to be monotonic. Live and offline produce byte-identical verdicts,
  scores and findings across 34 mid-stream snapshots on three real PX4 flights.
- ROS 2 `rclpy` source (`src/baglens/ros2.py`), tested against a stub node graph.
- `.ulg` writing in the synthetic generator, putting PX4's format into CI for the first
  time — all four formats now run in the format-equivalence test.
- Real ROS 2 corpus: 11 recordings across 5 platforms (`evals/integrity/ROS2_DATA.md`).

### Fixed

- Correlation precision on real flights: **0.381 → 0.942 at recall 0.993** across 105
  distinct PX4 flights and 152 dropouts the flight controller labelled itself
  (`evals/integrity/REAL_DATA.md`, regenerated by `evals/integrity/real_data.py`).
- The correlation interval cap ranked by duration, which evicted exactly the short
  high-concurrency intervals that *are* the stalls — 35 of 152 labelled dropouts, while
  `pytest` and both synthetic evals stayed green. It now ranks by concurrency.
- ULog timestamp `0` treated as unset rather than as t=0; two flights had been reading as
  56 years long, and 79 of 121 files were affected.
- `rate_degradation` could contradict its own numbers ("sped up by 65%, 1715.1 → 1650.3
  Hz") because direction came from the episode's peak slope while the rates were read
  from a bucket ring that had moved on. One finding per topic now, with the sentence
  derived from the two rates it prints.
- Per-topic state exceeded the published `<2 KB` budget by 3.6× (7,360 B on a 118-topic
  flight). `BAGLENS_EDGE_PROFILE=1` is 2,016 B and is gated in CI.
- Snapshot overhead was published as ~14% and had never been measured; it was +131%, and
  the cost was a quadratic `min()` inside `finish()` rather than serialisation. Now +116%.

[Unreleased]: https://github.com/ahmedsleem109/Baglens/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/ahmedsleem109/Baglens/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/ahmedsleem109/Baglens/releases/tag/v0.2.0
