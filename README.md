# baglens

**An MCP server that lets an agent investigate a fleet of robot logs — indexed, comparative, budget-aware, and citing its evidence.**

Existing rosbag MCP servers let an LLM *open a bag*. `baglens` audits whether the
recording can be trusted at all, remembers your whole corpus, and answers the question
that actually matters when something breaks: **has this happened before?**

```
> Audit ~/data/mission_204.mcap before I draw any conclusions from it.

  verdict: usable_with_caveats (score 77.5/100)

  CRITICAL  system-wide stall: 4 topics silent together for 5.19s at t=41.8s
  HIGH      /camera/image_raw silent for 5.19s (156x its 30.0 Hz period)
  MEDIUM    /imu/data is missing ~516 messages (5.7% of expected)

  caveats:
    - /camera/image_raw was silent between t=41.8–47.0s; do not compute rates,
      counts, or coverage over that window.
    - At least one silence was system-wide, so it reflects the recording host
      rather than any sensor; do not attribute those windows to a subsystem.

  /camera/image_raw |▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓█▓▓▓▓▓▓▓▓▓▓▓▓     ▓▓▓▓▓▓▓▓▓▓▓▓▓▓█▓▓▓▓▓▓▓▓▓▓▓▓▓▓|
  /imu/data         |█▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓█▓▓▓▓▓▓▓▓▓▓▓▓█▓▓▓▓    ·▓█▓▓▓▓█▓▓▓█▓▓▓▓▓█▓█▓▓▓▓▓▓▓▓▓|
  /odom             |▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓█▓▓▓▓▓▓▓▓▓▓▓█▓▓▓    ·▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓|
  /scan             |▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓█▓▓▓▓▓▓▓▓▓▓▓▓█▓▓▓▒    ·▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓█▓▓|
```

That last block is `health.topic_timeline`: four topics dying at the same instant, which
is the difference between "the camera failed" and "the recorder stalled". It costs about
300 tokens.

---

## Install

```bash
uvx --from git+https://github.com/yourname/baglens baglens --stdio
```

No ROS installation required — `.mcap`, `.db3`, `.bag` and PX4 `.ulg` are all read in
pure Python.

## Why this exists

| Project | What it does | Where it stops |
|---|---|---|
| `binabik-ai/mcp-rosbags` | Reads `.bag`, `.db3`, `.mcap`; message querying, trajectories, TF | One bag at a time; no persistent index; no cross-mission reasoning |
| `rosbags-mcp` + MCP Lab UI | Pulls data from bags, plots with Plotly | Per-file; plotting-oriented rather than investigation-oriented |
| ROSBag MCP Server (arXiv 2511.03497) | Academic; trajectories, laser scans, transforms; benchmarks 8 LLMs | Research release; mobile-robotics scoped; single-bag |
| `ros-mcp-server` / RobotMCP | **Live** robot control — publish to topics, call services | Not a log-analysis tool; the opposite direction |
| Foxglove | Best-in-class visualisation and data management | Human-in-the-loop GUI, not an agent tool surface |
| **baglens** | Integrity auditing, a durable fleet catalog, cross-mission comparison, provenance on every claim | Not a visualiser, not a robot controller, not a cloud platform |

The gap this fills: **nothing tells you your data is wrong.** `rosbag2` drops messages
under load — users report frame loss well below disk throughput limits — and you find
out three weeks later, mid-investigation. `baglens` finds it in the first thirty seconds
and tells the agent, in writing, what conclusions the recording cannot support.

## What it detects

Eight detectors, all single-pass with bounded state, measured against 200 synthetic
recordings with injected, labelled faults (160 faulted + 40 clean controls):

| Detector | What it catches | Precision | Recall | FP per clean bag |
|---|---|---|---|---|
| `gap` | A topic goes silent | 1.000 | 1.000 | 0.000 |
| `rate_degradation` | A topic slowly *slows down* — the precursor nothing else looks for | 1.000 | 1.000 | 0.000 |
| `jitter` | Timing variance widens before rates change | 1.000 | 1.000 | 0.000 |
| `dropped` | Messages missing, clustered or diffuse | 1.000 | 1.000 | 0.000 |
| `clock_lag` | The recorder falling behind the publishers | 1.000 | 1.000 | 0.000 |
| `clock_step` | NTP corrections and backward time | 1.000 | 1.000 | 0.000 |
| `correlation` | Sensor failure vs. system-wide stall | 1.000 | 1.000 | 0.000 |
| `file_integrity` | Truncated, unindexed and in-progress files | 1.000 | 1.000 | 0.000 |

Reproduce with `uv run python -m evals.integrity.run --regenerate`. Full method and the
matching rules are in [`evals/integrity/RESULTS.md`](evals/integrity/RESULTS.md).

**Read that table with the appropriate scepticism.** These are synthetic faults from a
generator in this repository. Perfect scores mean the detectors and the generator agree
about what a fault looks like — they are a regression gate and a floor, not evidence of
field accuracy. The number that would prove field accuracy is a real finding in someone
else's data, and until that lands this table is the honest maximum claim.

## The health score, in the open

```
topic_score = 100 · (1 − 0.30·gap_penalty − 0.35·drop_rate
                         − 0.20·jitter_excess − 0.15·degradation)
overall     = 0.5·min(topic_scores) + 0.3·mean(topic_scores) + 0.2·file_score

≥85 trustworthy   ·   60–85 usable with caveats   ·   <60 compromised
```

Weighting the *minimum* heavily is deliberate: one broken critical topic compromises an
investigation regardless of how healthy the other forty are. `gap_penalty` is measured
against 5% of the recording's length, so five missing seconds matter in a two-minute run
and not in an eight-hour one. Every constant lives in `config.py` and is overridable.

## The streaming constraint

Every detector is an **online algorithm with bounded state**. No detector buffers the
recording, needs the end time, or makes a second pass:

- statistics come from Welford and EWMA, never `numpy.mean` over an accumulated array;
- thresholds adapt from a warmup window, never from global file statistics;
- gap lists, lag curves and density timelines are all fixed-size, with truncation
  reported rather than hidden.

That costs perhaps 20% more effort and buys two things: 50 GB files audit without being
loaded, and the same code runs unchanged against a live subscription. Measured state is
**~3.1 KB per topic**; `BAGLENS_EDGE_PROFILE=1` shrinks the windows to fit under 2 KB.

## Performance, measured

On a 600-second, 8-topic, 158k-message recording (WSL2, single thread):

| Metric | Measured | Note |
|---|---|---|
| Arrival scan (payload-free) | 69,000 msg/s · 5.0 MB/s | no detectors, timing records only |
| Full audit, all 8 detectors | 25,000 msg/s · 1.8 MB/s | the number that matters |
| Peak RSS | 39 MB | independent of file size |
| State per topic | 3,136 B | 1,888 B under `BAGLENS_EDGE_PROFILE=1` |

MB/s is dominated by *message count*, not bytes: the audit never parses payloads, so a
bag full of camera frames audits far faster per megabyte than these tiny synthetic
messages do. The original 500 MB/s target in the design notes was written before
measurement and is not achievable in pure Python at this message rate — `scripts/bench.py`
asserts the real numbers instead.

## Tool surface

42 tools across 10 namespaces — see [`docs/tool-reference.md`](docs/tool-reference.md).

| Namespace | Purpose |
|---|---|
| `health.*` | Audit a recording, find gaps, clock report, timeline, validate, explain |
| `inspect.*` | Topics, schemas, samples, field statistics |
| `timeseries.*` | Extract, anomalies, changepoints, correlation, window comparison |
| `catalog.*` | Register sources, index, list, search, tag, fleet summary |
| `compare.*` | Mission diff, cohorts, find similar, regression scan, ranking |
| `logs.*` | Query, pattern clustering, merged timeline, log/signal correlation |
| `spatial.*` | Trajectory summary, deviation, TF tree health |
| `frames.*` · `pointcloud.*` | Keyframes, contact sheets, lidar statistics |
| `export.*` | Trim bag, Plotly HTML, Foxglove layout, cited Markdown report |

Design rules, enforced by a contract test that discovers new tools automatically:
every result is a typed model, carries provenance, and respects a token budget.

**Tool-surface eval:** 56 cases, 100% pass rate, 0.67% uncited claims, 773 tokens per
case on average — [`evals/RESULTS.md`](evals/RESULTS.md).

## Configuration

<details>
<summary>Claude Desktop on Windows, server in WSL</summary>

```json
{
  "mcpServers": {
    "baglens": {
      "command": "wsl.exe",
      "args": ["-d", "Ubuntu", "--cd", "/home/YOU/dev/baglens",
               "--", "/home/YOU/.local/bin/uv", "run", "baglens", "--stdio"],
      "env": {}
    }
  }
}
```

Use the **absolute path** to `uv`: WSL non-login shells often lack `~/.local/bin` on
`PATH`, and that failure is silent and maddening.
</details>

<details>
<summary>Claude Code / Cursor / native Linux and macOS</summary>

```json
{
  "mcpServers": {
    "baglens": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/yourname/baglens",
               "baglens", "--stdio", "--root", "/home/YOU/data"]
    }
  }
}
```
</details>

Ready-made copies live in [`examples/`](examples/).

| Flag / env | Effect |
|---|---|
| `--root DIR` (repeatable) | Confine all reads under DIR; traversal outside is refused |
| `--sensitivity low\|normal\|high` | Scales gap and trend thresholds for your fleet's noise floor |
| `--no-frames` | Disables all image extraction outright |
| `--http --port 8765` | Streamable HTTP instead of stdio |
| `BAGLENS_MAX_TOKENS` | Per-tool response budget (default 4000) |
| `BAGLENS_EDGE_PROFILE=1` | Shrink detector state under 2 KB/topic |
| `BAGLENS_REDACT_TOPICS` | Comma-separated topics masked before results leave a tool |

## Privacy and safety posture

- **Local-first, zero telemetry.** Nothing leaves your machine.
- **Read-only by construction.** The only writer in the codebase is `export.trim_bag`,
  and it only ever creates a new file.
- **Root confinement** and **config-driven redaction** applied before results leave the
  tool boundary, so masked fields never reach the model.

## Non-goals

- **Not a visualiser.** Foxglove exists and is better; `export.foxglove_layout` hands off.
- **Not a robot controller.** Read-only, offline, forensic.
- **Not a cloud platform.** No account, no upload, no telemetry.
- **Not a storage format.** MCAP won. Consume it.

## Development

```bash
git clone https://github.com/yourname/baglens && cd baglens
uv sync
uv run pytest -q                                    # 54 tests
uv run python -m tests.synth.generate --matrix --out /tmp/bags
uv run python -m evals.integrity.run --bags /tmp/bags    # precision/recall
uv run python -m evals.runner                            # tool-surface eval
uv run python scripts/bench.py                           # performance gates
```

Docs: [quickstart (WSL)](docs/quickstart-wsl.md) · [tool reference](docs/tool-reference.md) ·
[recipes](docs/recipes.md) · [design notes](docs/design-notes.md)

Apache-2.0.
