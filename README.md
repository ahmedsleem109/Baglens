# baglens

**Checks whether a robot recording can be trusted, before you draw conclusions from it.**

A robot records everything it does to a file. Something goes wrong on Tuesday; on
Wednesday an engineer opens that file to find out why. They see the camera stop for five
seconds right before the crash. Obvious — the camera failed. Two days later, having
replaced it, they learn the camera was fine: the recording computer couldn't keep up
writing to disk and dropped five seconds.

**Nothing marked it as missing. It's just absent, and the file looks perfectly normal.**

`baglens` reads the file first and says: there's a five-second hole at t=41s, and 105
other topics went silent in the same instant, so this was the recorder — not the camera.
Don't conclude anything about the camera from that window.

![baglens auditing a real PX4 flight](docs/assets/demo.gif)

That's a real public PX4 flight, through the same entry point an MCP client uses.

## Try it

```bash
uvx --from git+https://github.com/ahmedsleem109/Baglens baglens --stdio
```

No ROS installation needed. Reads `.mcap`, rosbag2 `.db3`, ROS 1 `.bag` and PX4 `.ulg` in
pure Python, and a test asserts all four reach identical conclusions on the same
recording. To wire it into Claude, Cursor or any MCP client, see
[configuration](docs/configuration.md).

## Three ways people use it

**Debugging an incident.** Audit the bag before you open it in Foxglove, so you don't
spend two days on a sensor that never failed. Every finding carries the evidence that
supports it — the list of topics that went quiet together *is* the diagnosis.

**Building robot training data.** `baglens gate` sorts a dataset into episodes that are
safe to train on and episodes that aren't, with a reason for every rejection.

**Giving an AI agent honest data.** It's an MCP server, so an agent calls it first and
gets back explicit `caveats` — written statements of what the recording *cannot* support.
That prevents a large class of confident, wrong answers.

## Does it actually work?

The uncomfortable version, which is the point:

| Tested against | Labels | Recall | Precision |
|---|---|---|---|
| Synthetic faults we generated | 200 bags | 1.000 | 1.000 |
| **The same faults injected into real recordings** | **34** | **0.824** | **1.000** |
| PX4's own dropout records — labels we didn't write | 152 | 0.993 | 0.955 |

**A real background costs 18 points of recall.** The perfect synthetic score only proves
the detectors and the fault generator agree with each other. The middle row is the honest
one, and it is published rather than buried.

Details, method, and what these numbers do *not* prove:
[`INJECTED.md`](evals/integrity/INJECTED.md) ·
[`REAL_DATA.md`](evals/integrity/REAL_DATA.md) ·
[`RESULTS.md`](evals/integrity/RESULTS.md)

## When it won't answer

Sometimes a recording can't be judged at all — a robot that sat parked, a file too short
to establish what "normal" looks like. Then the verdict is `unassessable`, with reasons,
instead of a score.

Both recordings below are the same shuttle bus, in the same run of the same tool. One was
driving; one was parked. It grades the first and declines to grade the second:

![baglens refusing to grade a recording it cannot measure](docs/assets/demo-refuse.gif)

That comparison is the point. A refusal only means something if the same tool confidently
grades the recording next to it. **A short list of findings is not proof that a recording
is healthy — it can just as easily mean nothing was measurable.**

## How old was the data behind that command?

Rate is the wrong question. `/camera` publishing a steady 30 Hz tells you nothing about
whether the frame behind the last steering command was 80 ms old or 300 ms old — and that
difference is the one that makes a robot overshoot.

ROS messages carry the time the data was *captured*, and nodes pass that stamp along as
they derive results from it. `baglens` follows it and reports the age per stage:

```
/camera/image_raw    P50   12 ms   P95   14 ms   P99   16 ms
/detections          P50   94 ms   P95  112 ms   P99  121 ms   (+82 ms, from /camera)
/cmd_vel_stamped     P50  131 ms   P95  150 ms   P99  159 ms   (+37 ms, from /detections)
```

Nothing declares that chain — it is inferred from stamp equality.

**A caveat measured rather than assumed:** across 11 real public recordings, that chain
mostly does *not* survive to the actuator. What real robots share stamps for is sensor
synchronisation — stereo pairs, hardware-synced lidar — and driver-internal steps. Even a
full Nav2 shuttle bus restamps before `/cmd_vel`. So on most recordings this reports
per-topic age plus **which node broke the trace**, and the end-to-end chain above needs a
stack that propagates stamps. [The numbers](docs/how-it-works.md#what-stamp-propagation-actually-looks-like-on-real-robots).

When a stage cannot be measured, it is **named**, never filled in with the arrival time:

```
/cmd_vel             unmeasurable — geometry_msgs/msg/Twist carries no header.stamp
```

That refusal is the whole point. A `geometry_msgs/Twist` has no stamp, so the chain breaks
exactly at the actuator — which is where you most want it — and the honest answer is to
say so. The same applies to a node that restamps with its own clock, to a topic stamped
from a monotonic clock, and to a recording whose publishers disagree about the time: each
is reported as what it is rather than converted into a plausible-looking number.

Reading the stamp costs an 8-byte peek, not a decode — verified against a full decode on
134 topics across 11 real recordings, zero disagreements.

## TF, diagnosed instead of drawn

`ros2 run tf2_tools view_frames` gives you a PDF of the transform tree and leaves you to
find the problem in it. This gives you the problem:

```bash
baglens frames mission.mcap
```

```
1 transform finding(s):
  HIGH     map→odom is published by more than one source, disagreeing by up to 0.35 m
           Two nodes are fighting over one transform. Consumers see the pose flip
           between them depending on which arrived last, and nothing in ROS reports it.

tree: 4 transform(s), roots ['map']
               base_link -> camera         static
               base_link -> laser          static
                     map -> odom           22 Hz  2 publishers ±0.35 m
                    odom -> base_link      50 Hz
```

It catches the four that cost the most: **two nodes fighting over one transform**, a
**frame nothing provides** (the static transform nobody launched), transforms **stamped
into the future**, and a chain that is **complete only intermittently**.

`--out tree.pdf` renders the same analysis as a printable page with the broken edges
coloured — no graphviz needed, the PDF is written directly. `--json`, and the
`health.transform_health` MCP tool, hand the whole thing to an agent as structured data,
so it can act on the diagnosis rather than parse a picture. Non-zero exit when something
is wrong, so it drops straight into CI.

## Don't burn the field day

```bash
baglens preflight --expect fleet_baseline.json --for 30s
```

A test day costs thousands and gets burned because a node didn't launch, a sensor came up
in the wrong mode, or a topic was already degrading before anyone drove anywhere. You find
out that evening, in the bag.

Thirty seconds of watching the live graph, then one answer — **GO** or **NO-GO**, with
reasons and an exit code you can put in a launch file or a CI job:

```
NO-GO — 5,549 messages in 0.4s

2 reason(s) not to fly:
  FAIL  /scan: 150 of ~300 expected messages (50%); silent for 0.0s of 30s
  FAIL  /scan: 5.02 Hz vs 10.02 Hz baseline (-50%)
```

The baseline isn't hand-written — `preflight --record` captures it from a run that was
known good, so "normal" is what your robot actually does rather than what a datasheet
claims. And anything the gate could not check in thirty seconds is reported as
**unchecked**, never as a pass: a gate that quietly waves through what it didn't measure
is worse than no gate at all.

Zero false alarms across ten healthy graphs — the number that decides whether anyone
leaves it switched on.

## The training-data gate

```bash
baglens gate ~/data/episodes --out manifest.json --max-gap 0.5
```

Nobody minds a slightly lossy debug bag. A training set is different: when the recorder
stalls for 200 ms, nothing errors — the action at *t* is quietly paired with an
observation from *t−200 ms*, and the model learns that. You find out weeks later, in
evaluation, having already paid for the training run.

So the output isn't a score. It's a manifest: accept / review / reject per episode, a
reason for every rejection, and a `train_on` list your training job reads directly.

![baglens gating a dataset of episodes](docs/assets/demo-gate.gif)

"`/sbg/ekf_nav` was silent for 15.01s in one stretch" is actionable. "Score 61" is not.

**Scope.** This reads recordings with real timestamps. It does **not** audit
LeRobot-format datasets — those recompute per-frame timestamps as `frame_index / fps`
during conversion, so the timing evidence is already gone. Gate the recordings, then
convert.

## What's under it

- **Eight detectors**, each single-pass with bounded state: gaps, rate degradation,
  jitter, dropped messages, clock lag, clock steps, cross-topic correlation, file
  integrity. Nothing buffers the file, so a 50 GB recording audits without being loaded
  and the same code runs against a live subscription.
- **End-to-end data age**, per stage, over a propagation graph inferred from stamp
  equality rather than declared — with every stage it cannot measure named as such.
- **43 MCP tools** across 10 namespaces — audit, inspect, timeseries, catalog, compare,
  logs, spatial, frames, export. Every result is typed, carries provenance, and respects
  a token budget.
- **Local-first and read-only.** No account, no upload, no telemetry. `--root` confines
  every read to directories you name; `BAGLENS_REDACT_*` masks topics and fields before
  anything reaches the model.

[How it works](docs/how-it-works.md) — the score formula, the streaming constraint,
measured performance, and the measurements that went the wrong way.

## Not this

Not a visualiser (Foxglove is better; `export.foxglove_layout` hands off). Not a robot
controller — read-only and offline. Not a cloud platform. Not a storage format.

## Development

```bash
git clone https://github.com/ahmedsleem109/Baglens && cd baglens
uv sync
uv run pytest -q                                          # 253 tests
uv run python -m evals.integrity.run --regenerate         # precision/recall
uv run python scripts/bench.py                            # performance gates
```

[quickstart (WSL)](docs/quickstart-wsl.md) · [tool reference](docs/tool-reference.md) ·
[recipes](docs/recipes.md) · [how it works](docs/how-it-works.md) ·
[configuration](docs/configuration.md) · [design notes](docs/design-notes.md) ·
[changelog](CHANGELOG.md)

Apache-2.0.
