# What causes the recorder stalls? — a negative result

*Hand-written, not generated. Reproduce with `scripts/study_stall_attribution.py`.*

The auditor reports that ~115 topics went silent together for a few seconds. The obvious
next question is *why*, and `ENHANCEMENTS.md` originally answered it with a confident
example:

> *"All 54 logging blackouts coincide with `cpuload` > 0.92 and follow an SD write burst.
> This is storage backpressure."*

**That sentence was invented.** It was plausible, it was never measured, and when
measured it turned out to be wrong in both halves. This document is what replaced it.

## Corpus

103 distinct public PX4 flights from review.px4.io, deduplicated by content hash,
**1,935 blackouts**. A blackout is an interval longer than 100 ms in which *not one* of
the ~115 recorded topics produced a sample — a hole that no single sensor failure can
explain.

Two numbers below come from the raw exploratory analysis over individual blackouts, and
the rest from the shipped kernel, which **merges overlapping blackouts** before measuring.
Merging lowers the clustering statistic (5.10 raw → 2.95 merged) because a burst of
adjacent holes becomes one event. Both are stated where they differ; the kernel's figure
is the reproducible one.

## Method

For each blackout, sample each candidate signal in the 3 s **before** the onset and
compare against that signal's own baseline elsewhere in the same flight, reported as
Cohen's *d*. Sampling *inside* a blackout is circular: nothing publishes there.

Two guards matter, and both were added because the first run was wrong without them:

1. **Exclude samples inside other blackouts.** Blackouts cluster hard (below). The naive
   pre-window is full of neighbouring blackouts whose empty bins drag the pre-mean toward
   zero. Without this guard the analysis produced a spectacular *d* = −5.94 with 100%
   sign agreement — a measurement of the clustering, not of any cause.
2. **Require per-blackout consistency, not just an aggregate shift.** See below.

## Results

| Hypothesis | Result |
|---|---|
| CPU load elevated before blackouts | **Not supported.** mean *d* = −0.21 (slightly *lower*), sign agreement 57% — a coin flip |
| Message-volume burst before blackouts | **Not supported.** mean *d* = −0.084, \|*d*\| > 0.5 in 4% of logs |
| `logger_status` (PX4's own backpressure telemetry) | **Unavailable.** Absent from 0/102 logs — not enabled in default logging profiles |
| Blackouts are periodic (scheduled flush) | **Not supported.** No log had a dispersion below 0.5 |
| **Blackouts are clustered** | **Supported.** Index of dispersion mean 2.95 as the kernel measures it (5.10 over unmerged blackouts); 1.0 would be random/Poisson. 58 of the 87 classifiable flights read as clustered, 29 as random, none as periodic |

Recording time lost to blackouts across the corpus: **mean 28.6%**, with >5% lost in
79% of logs.

### The multiple-comparisons trap

Testing 5 signals across ~100 flights is ~500 tests. At a |*d*| > 0.5 threshold the
kernel initially attributed a cause in **12 of 102** logs — but the directions
contradicted each other between logs (`cpuload.load` positive in one, negative in
another; likewise battery voltage). Those were chance hits, not causes.

Adding the requirement that a signal must shift **the same way before ≥70% of individual
blackouts**, not merely move the aggregate mean, cut this to **3 of 103** — and two of
those three are battery voltage in *opposite* directions, so ~3% is about the residual
chance rate rather than a real discovery.

A further 76 of 103 flights return `no_data`: once stall-interior samples and the ±5 s
guard are excluded, the low-rate telemetry (`cpuload` is ~5 Hz) no longer has enough
baseline left to test against. That is a real limitation of the method on stall-heavy
recordings, and it is reported as `no_data` rather than folded into "unexplained".

## Conclusion

Across 103 real flights, **nothing recorded in the log explains the blackouts.** They are
strongly clustered in time, which points at a shared external resource — storage,
a bus, thermal throttling — rather than at any node or at CPU pressure. That is as far as
the data supports, and further than the data supports is where the original claim went.

The clustering is itself the most useful thing we can report: it rules out the
per-sensor explanations an engineer would otherwise chase first.

## What shipped

`src/baglens/kernels/attribution.py` and the `health.explain_stalls` tool. Its design
follows directly from the above:

- Ranks candidate signals by effect size, with the neighbouring-blackout guard built in.
- Requires per-stall consistency before calling anything an explanation.
- Classifies the temporal pattern (clustered / periodic / random) and says what that
  implies, which is the answer that actually survives on this corpus.
- **Returns `unexplained` and `no_data` as first-class verdicts**, with the reason. On
  this corpus that is the honest answer for 100 of 103 flights.

A kernel that always names a cause would have shipped the sentence at the top of this
document to every user.
