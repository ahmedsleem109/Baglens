# Configuration

## Wiring it into an MCP client

<details open>
<summary><b>Claude Code / Cursor / native Linux and macOS</b></summary>

```json
{
  "mcpServers": {
    "baglens": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/ahmedsleem109/Baglens",
               "baglens", "--stdio", "--root", "/home/YOU/data"]
    }
  }
}
```
</details>

<details>
<summary><b>Claude Desktop on Windows, server in WSL</b></summary>

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

Use the **absolute path** to `uv`. WSL non-login shells often lack `~/.local/bin` on
`PATH`, and that failure is silent and maddening.
</details>

Ready-made copies live in [`examples/`](../examples/), and there is a longer walkthrough
in [quickstart (WSL)](quickstart-wsl.md).

## Flags and environment

| Flag / env | Effect |
|---|---|
| `--root DIR` (repeatable) | Confine all reads under DIR; traversal outside is refused |
| `--sensitivity low\|normal\|high` | Scales gap and trend thresholds for your fleet's noise floor |
| `--no-frames` | Disables all image extraction outright |
| `--http --port 8765` | Streamable HTTP instead of stdio |
| `BAGLENS_MAX_TOKENS` | Per-tool response budget (default 4000) |
| `BAGLENS_EDGE_PROFILE=1` | Shrink detector state under 2 KB/topic |
| `BAGLENS_REDACT_TOPICS` | Comma-separated topics dropped before results leave a tool (`/camera/*` matches a prefix) |
| `BAGLENS_REDACT_FIELDS` | Comma-separated `field.path` or `/topic:field.path` rules; masked in payloads *and* refused through `field_stats` and `timeseries.extract` |

Every detector threshold lives in `src/baglens/config.py`, documented, and is overridable
with a `BAGLENS_`-prefixed environment variable.

## The training-data gate

```bash
baglens gate <dir> [--out manifest.json]
```

| Flag | Default | Effect |
|---|---|---|
| `--require a,b` | none | Topics every episode must contain. Loss and stall limits then apply to these rather than to every topic — so a noisy diagnostics channel can't block an episode you don't train on. |
| `--max-gap SECONDS` | off | Longest single silence allowed. **The one worth setting deliberately:** fractions are the only scale-free default, but a 15-second hole in a 31-minute recording is 0.8% and passes every fraction limit while being fifteen seconds the policy never sees. For 30 fps demonstration data, set it under a second. |
| `--max-stall-fraction` | 0.02 | Share of the episode inside a system-wide recorder stall |
| `--max-drop-fraction` | 0.05 | Estimated message loss on a required topic |
| `--max-lag SECONDS` | 1.0 | How far the recorder may fall behind the publishers |
| `--min-score` | off | Health score floor |
| `--accept-unassessable` | off | Flag rather than reject episodes that could not be assessed |
| `--strict` | off | Exit non-zero unless *every* episode was accepted, not just on rejections |

Exit code is 0 when nothing was rejected, 1 otherwise — so it drops into CI unchanged.
The manifest records the policy it ran under, so a stricter run stays distinguishable from
a worse dataset.

## Privacy and safety posture

- **Local-first, zero telemetry.** Nothing leaves your machine.
- **Read-only by construction.** The only writer in the codebase is `export.trim_bag`, and
  it only ever creates a new file.
- **Root confinement** and **config-driven redaction** are applied at the tool boundary, so
  masked fields never reach the model — including through `field_stats` and
  `timeseries.extract`, which would otherwise be a way around the mask.

If you audit recordings you did not produce, use both:

```bash
baglens --root /srv/recordings --no-frames
```

See [SECURITY.md](../SECURITY.md) for the threat model and how to report an issue.
