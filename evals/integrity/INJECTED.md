# Detector performance on real recordings with injected faults

Corpus: **5 real recordings** from `~/data/public/ros2` (shuttle bus, teleop arm rig, Tesla Model 3 CAN, handheld LIVO), each copied clean and once per fault class — **39 copies, 34 exact labels**, 3.9 GB. Generated 2026-08-16.

**The background is real; the fault is ours.** Every copy keeps the source's own jitter, burstiness, topic mix and QoS metadata. `tests/synth/inject.py` then removes, thins, stretches or shifts one known window. That is the cell neither `RESULTS.md` (synthetic background) nor `REAL_DATA.md` (one detector, one fault class) fills.

**Scoring is differential.** Each base is also audited clean, and its findings become a baseline — a real recording has findings of its own, and counting those against the detector would measure the recording, not the detector. Only findings that appear *with* the fault are attributed to it.

| Metric | Value |
|---|---|
| Injected labels | 34 |
| **Recall** | **0.824** |
| **Precision** | **1.000** |
| Attributed false positives | 0 |
| Baseline findings excluded | 18 |

## Per detector

| Detector | Labels | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|---|
| `gap` | 5 | 4 | 0 | 1 | 1.000 | 0.800 | 0.889 |
| `rate_degradation` | 2 | 1 | 0 | 1 | 1.000 | 0.500 | 0.667 |
| `jitter` | 2 | 2 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| `dropped` | 5 | 3 | 0 | 2 | 1.000 | 0.600 | 0.750 |
| `clock_lag` | 5 | 5 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| `clock_step` | 5 | 4 | 0 | 1 | 1.000 | 0.800 | 0.889 |
| `correlation` | 5 | 4 | 0 | 1 | 1.000 | 0.800 | 0.889 |
| `file_integrity` | 5 | 5 | 0 | 0 | 1.000 | 1.000 | 1.000 |

## Per recording

| Recording | Duration | Topics | Baseline findings | Variant | Label caught |
|---|---|---|---|---|---|
| `dongkkka_00` | 37s | 11 | 0 | `dropout` (topic_dropout) | yes |
| | | | | `jitter` (jitter_injection) | yes |
| | | | | `lag` (recorder_lag) | yes |
| | | | | `stall` (correlated_stall) | yes |
| | | | | `step` (clock_step) | yes |
| | | | | `thin` (diffuse_drops) | yes |
| | | | | `truncate` (truncation) | yes |
| `fastlivo_hku2` | 16s | 4 | 0 | `dropout` (topic_dropout) | yes |
| | | | | `lag` (recorder_lag) | yes |
| | | | | `stall` (correlated_stall) | yes |
| | | | | `step` (clock_step) | yes |
| | | | | `thin` (diffuse_drops) | yes |
| | | | | `truncate` (truncation) | yes |
| `nuway_stops` | 131s | 70 | 16 | `degrade` (rate_degradation) | **no** |
| | | | | `dropout` (topic_dropout) | **no** |
| | | | | `lag` (recorder_lag) | yes |
| | | | | `stall` (correlated_stall) | **no** |
| | | | | `step` (clock_step) | **no** |
| | | | | `thin` (diffuse_drops) | **no** |
| | | | | `truncate` (truncation) | yes |
| `nuway_waypoints` | 1843s | 5 | 2 | `degrade` (rate_degradation) | yes |
| | | | | `dropout` (topic_dropout) | yes |
| | | | | `jitter` (jitter_injection) | yes |
| | | | | `lag` (recorder_lag) | yes |
| | | | | `stall` (correlated_stall) | yes |
| | | | | `step` (clock_step) | yes |
| | | | | `thin` (diffuse_drops) | yes |
| | | | | `truncate` (truncation) | yes |
| `tesla3_av` | 16s | 13 | 0 | `dropout` (topic_dropout) | yes |
| | | | | `lag` (recorder_lag) | yes |
| | | | | `stall` (correlated_stall) | yes |
| | | | | `step` (clock_step) | yes |
| | | | | `thin` (diffuse_drops) | **no** |
| | | | | `truncate` (truncation) | yes |

## The first run, and what changed

The first version of this corpus scored **0.615 recall / 0.800 precision over 39 labels**, and the number is recorded here because the reason it moved is the kind of thing that usually disappears from a README.

Nothing about the detectors changed — no threshold, no rule, no line of `src/baglens`. What changed is that the corpus had been writing labels the detectors do not claim to be able to satisfy:

* Four of five `rate_degradation` labels sat on recordings shorter than D3's minimum history (`min_buckets * bucket_s` = 80 s), and the fifth spread its ramp across 1843 s when D3 can only see 300 s of slope at a time.
* Three of five `recorder_lag` labels grew less total lag than D6's 100 ms floor, because the growth was specified per minute and applied to a 16-second slice.
* Two `jitter` labels targeted topics with fewer messages than D4's variance window, so no baseline could exist to expand.
* Six findings counted as false positives were consequences of the injection method rather than of the detectors: moving `log_time` and leaving `publish_time` alone genuinely reorders the two, and the clock detector was correct to say so.

A label a detector could not satisfy measures the length of the recording, not the accuracy of the detector. Fault magnitudes are now matched to the synthetic corpus so the two numbers compare, and faults whose floor a recording cannot clear are not written — which is why the label count fell from 39 to 34.

## What this measures, and what it does not

It measures whether a fault of a known shape, placed at a known time in a real recording, is found by the detector that claims that shape. It does **not** measure whether every naturally-occurring fault is found: injection can only produce fault shapes someone thought of, and a recording's own defects are excluded by the baseline rather than scored.

Two labels are weaker than the rest and are marked as such wherever they matter. `diffuse_drops` and `recorder_lag` are whole-run faults, so any finding of the right kind anywhere in the recording matches them — a coarser test than the windowed faults get. And a fault injected into a topic the recording barely publishes is not detectable in principle; `inject.plan_for` only targets topics above 1 Hz for that reason.

Reproduce: `uv run python -m tests.synth.inject --sources ~/data/public/ros2 --out /home/sleem/data/injected` then `uv run python -m evals.integrity.injected --bags /home/sleem/data/injected`.

