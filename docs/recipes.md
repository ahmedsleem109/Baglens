# Recipes

Real investigations, copy-pasteable. Each one names the tools in the order that actually
works, because the ordering is most of the skill.

---

## 1. "Can I trust this recording at all?"

```
health.audit_recording(path)
```

Read `verdict` and `caveats` first, findings second. The caveats are the auditor telling
you what conclusions this data *cannot* support — they are what stop a confident wrong
answer three steps later.

If `verdict` is `compromised`, stop and fix the data before analysing it.

---

## 2. "A topic went quiet — did the sensor die or did the recorder stall?"

```
health.find_gaps(path)                 → classification + co_silent_topics
health.topic_timeline(path)            → see the shape
```

- `classification: isolated_topic` → that sensor or node.
- `classification: system_wide_stall` → the recording host: CPU, disk, power. Looking at
  any single topic here sends you down the wrong path.
- `classification: subsystem_failure` → **read `co_silent_topics`, that list is the
  diagnosis.** If `/camera/left`, `/camera/right` and `/camera/depth` died together, it
  is the driver or the USB bus, not three sensors.

---

## 3. "Frames are missing but the disk was nowhere near saturated"

```
health.clock_report(path)              → the recorder-lag curve
```

A rising `lag_curve_s` means the recorder was falling behind the publishers: messages
queue, the queue overflows, and nothing reports it. This is the mechanism behind the
well-known `rosbag2` frame-loss complaint. `lag_growth_s` over 100 ms across a run is
worth acting on; the shape of the curve tells you *when* it started.

Cross-check with:

```
health.audit_recording(path)           → look for a `dropped` finding with low agreement
```

Low agreement between the two drop estimators means loss was diffuse rather than
clustered — the signature of backpressure, not of a sensor outage.

---

## 4. "Something feels slow near the end of the run"

```
health.audit_recording(path)           → rate_degradation findings
timeseries.extract(path, topic, field, bin_s=5)
timeseries.detect_changepoints(path, topic, field)
```

`rate_degradation` fires on a *sustained trend*, not on a dip: a 30 Hz topic drifting to
22 Hz over ten minutes. Nothing else looks at rate as a trend, which is why this failure
mode usually gets found by a human noticing "it feels laggy".

---

## 5. "The control loop is unstable but the rates look fine"

```
health.audit_recording(path)           → jitter findings, and jitter_cv per topic
logs.correlate_with_signal(path, topic, field, level="WARN")
```

Timing variance widens before mean rate changes. `jitter_cv` is reported for every topic
in every audit whether or not it fires — it is a cheap number engineers read instantly.

---

## 6. "This file will not open in anything"

```
health.validate_file(path)             → never raises
health.audit_recording(path)           → audits what exists
```

Truncated, unindexed, in-progress and CRC-damaged files all degrade rather than fail. You
get how far it could read, how many bytes are lost, and which time range is untrustworthy.
Absence of data after the last readable record is *not* evidence of absence of events, and
the caveat says so.

---

## 7. "Has this happened before?"

```
catalog.add_source(dir)                → index once
catalog.index_status()                 → wait intelligently
compare.find_similar(mission_id, signal_key="/odom.twist.twist.linear.x")
```

Two passes: a cheap fingerprint (duration, topic set, rate profile, signal means,
log-template overlap) shortlists, then banded DTW ranks the shortlist. Each match comes
back with `why`.

---

## 8. "What changed after the v2.4 firmware?"

```
catalog.tag_mission(mission_id, "fw:2.4")     # for each, or use a date split
compare.cohorts(split_by="date", split_date="2026-06-01", metric="health_score")
compare.regression_scan()
```

`compare.cohorts` gives effect sizes across the split; `regression_scan` sweeps every
metric for a trend nobody filed a ticket about.

---

## 9. "Which recordings should I look at first?"

```
catalog.fleet_summary()                → verdict distribution + worst offenders
compare.rank_missions(metric="critical_events", ascending=false)
```

---

## 10. "Show me what the camera saw when it happened"

```
health.audit_recording(path)           → get t_start of the finding
frames.contact_sheet(path, start_s=t-5, end_s=t+5, count=9)
```

One tiled image with burnt-in timestamps costs far less context than nine separate
images and is easier for a vision model to compare across.

---

## 11. "Is the transform tree healthy?"

```
spatial.tf_report(path)
```

Stale and missing transforms are a classic silent killer: everything downstream keeps
using the last known pose and nothing complains. `problems` lists links that went stale,
jumped more than a metre, or stopped publishing and never resumed.

---

## 12. "Write this up for the issue tracker"

```
export.report(path)                    → Markdown, every claim cited
export.trim_bag(path, t-10, t+10)      → a shareable slice, not a 40 GB file
export.foxglove_layout(path, topics, focus_s=t)
```

The report carries the verdict, the caveats, every finding with the rule that fired and
the numbers behind it. The trimmed bag is the evidence. The layout hands off to the tool
your colleagues already use.

---

## 13. "Which of these episodes can I train a policy on?"

```bash
baglens gate ~/data/episodes --out manifest.json \
    --require /observation/joint_states,/action --max-gap 0.5
```

Not a score — a decision per episode with a reason code, plus a `train_on` list your
training job can read directly:

```
412 episodes under ~/data/episodes
  accept 388   review 5   reject 19
  8214s safe to train on, 402s withheld
  rejections:
       11  message_loss
        5  recorder_stall
        2  clock_non_monotonic
        1  unassessable
```

The limits worth setting deliberately:

* `--require` names the topics you actually train on. Loss and stall limits then apply to
  those rather than to a diagnostics channel nobody reads.
* `--max-gap` is the one this tool cannot guess. Fractions are the only scale-free
  default, but a 15-second hole in a 31-minute recording is 0.8% and passes every
  fraction limit — while being fifteen seconds the policy will never see. For 30 fps
  visuomotor data, set it under a second.
* An episode that comes back `unassessable` is not a bad episode; it is one where too
  little could be measured to tell. Rejecting those by default is deliberate: accepting
  them is how an unaudited episode reaches a training set wearing a clean label.

Commit the manifest next to the dataset and diff it when the dataset changes. It records
the policy it was produced under, so a stricter run is distinguishable from a worse
dataset.

**Scope.** This reads recordings with real timestamps — `.mcap`, `.db3`, `.bag`, `.ulg`.
It does not audit LeRobot-format datasets: those recompute per-frame timestamps as
`frame_index / fps` during conversion, so the timing evidence these detectors read is
already gone. Measured across `lerobot/pusht` and `lerobot/aloha_static_coffee`, the
inter-frame deltas vary only by float rounding (~1e-6 s). Gate the recordings, then
convert.
