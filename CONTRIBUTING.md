# Contributing

## Setup

```bash
git clone https://github.com/ahmedsleem109/Baglens && cd baglens
uv sync --dev
uv run pytest -q
```

Develop on the Linux filesystem (not `/mnt/c`) if you are on WSL — see
[docs/quickstart-wsl.md](docs/quickstart-wsl.md).

## The one rule that is not negotiable

**Detectors are single-pass with bounded state.** No detector may buffer the recording,
require the end time, or make a second pass. If the only formulation you can see is
offline, say so in the PR and stop — do not ship the offline version with a TODO. The
reasoning is in [docs/design-notes.md](docs/design-notes.md).

`tests/integration/test_detectors.py` asserts both halves of this: `state_bytes()` stays
under budget, and the auditor calls `arrivals()` exactly once.

## Adding a detector

In this order, and please keep the order:

1. **Write the fault generator first**, in `tests/synth/generate.py`, with its ground
   truth. A detector validated against a fixture written afterwards is validated against
   its own assumptions.
2. Write the detector in `src/baglens/detectors/`, exposing `on_arrival`, `finalize` and
   `state_bytes`.
3. Wire it into `Auditor` and add its target to `evals/integrity/run.py`.
4. **Publish its precision and recall** before moving on:
   `uv run python -m evals.integrity.run --regenerate`.

Every threshold goes in `config.py`, documented and overridable. Hard-coded constants in
detector bodies will be asked about in review.

## Adding a tool

Contract tests discover new tools automatically, so a tool that does not comply will fail
CI without anyone having to notice it:

- typed Pydantic input **and** output — the schema is what the model reads, so treat it
  as UX;
- a `provenance` field on anything reporting on data;
- a token budget with a reduction ladder and a `suggested_narrowing` message;
- a description written **for a model**: when to use it, when not to, and what the next
  tool usually is. One-line descriptions fail the contract test.
- names are `namespace.verb_noun`.

Then regenerate the reference: `uv run python scripts/gen_tool_reference.py`.

## Nothing writes to a recording

Read-only is a safety property this project advertises. The only writer is
`export.trim_bag`, and it only ever creates a new file. Keep it that way.

## Before opening a PR

```bash
uv run ruff check .
uv run pytest -q
uv run python -m evals.runner            # tool-surface eval, 56 cases
uv run python scripts/bench.py           # performance gates
```

If you changed a detector, also run the precision/recall matrix and paste the table into
the PR. A detector that drops below target fails the build, and that is deliberate.

## Good first issues

- A reader for a format we do not cover (ROS 1 `.bag` recovery, ULog edge cases).
- Real recordings that break something — a bag that our auditor gets wrong is the most
  valuable contribution here, more than any feature.
- Threshold tuning from your own fleet: if `sensitivity` is wrong for your robots, that
  feedback *is* the labelled dataset we do not otherwise have.
