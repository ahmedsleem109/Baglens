# Design notes

Why the parts are shaped the way they are, including the places where measurement
disagreed with the plan.

## The detectors are streaming because the offline version is a dead end

Every detector is a single-pass online algorithm with fixed-size state. The rule is
absolute: no detector may buffer the recording, require the end time, or make a second
pass. The reasons, in order of how much they mattered:

1. **The same code has to run live.** The long-term target is a device watching a robot
   in real time. A detector that only works on a closed file is the wrong detector, and
   rewriting it later means re-validating it later.
2. **It is better offline too.** Single-pass detectors audit 50 GB files without loading
   them. The naive versions cannot.
3. **It forces honest statistics.** You cannot take a global mean, so you take a warmup
   baseline — which is what a real deployment would do anyway.

Concretely: Welford and EWMA instead of `numpy.mean` over an accumulated array; log-spaced
histograms instead of sorting inter-arrivals; Theil–Sen over 30 buckets instead of a fit
over every sample; a decimating ring for the lag curve instead of resampling at the end;
a doubling-bucket density map instead of choosing column width once the duration is known.

The one place this shows visibly in the API: `FileIntegrity.last_readable_time` is an
epoch timestamp, because the structural validator runs standalone and has no bag-relative
clock. The auditor converts it. Getting that wrong cost a full eval run — the finding
window landed at t=1.78e9 and matched nothing.

## Why the mode, not the mean

The cadence baseline uses the modal inter-arrival from a 64-bin log-spaced histogram, not
the mean, because **the mean is destroyed by exactly the gaps you are trying to find.**
A 100 Hz topic with twenty 5-second gaps has a mean inter-arrival of 200 ms — five times
the truth — and every downstream threshold then scales wrong.

But the modal *bin* is only accurate to its own width (~15% at 64 bins), and a 4% bias in
the period turns directly into a 4% phantom drop rate. So the histogram is used only to
reject gaps, and the value comes from the median of the ring buffer inside that band. That
one change removed every false positive on the clean control set.

## Noise floors are not fudge factors

`expected_hz` is itself an estimate from ~50 warmup samples, carrying relative error
around `cv/√n`. Reporting a drop rate smaller than the uncertainty in the rate that
produced it is how a detector loses trust on healthy data. So `dropped` suppresses
anything below `max(2%, 4·cv/√n)` and reports the floor in its evidence.

The uncertainty must come from the *on-cadence* jitter, not raw inter-arrival CV — with
20% of messages dropped, raw CV is inflated by the very fault being measured, and an
early version of this floor silenced the detector completely.

## Sharing state between detectors

D1 (cadence) and D4 (jitter) both want a rolling variance of inter-arrival. Keeping two
copies was the largest line in the per-topic budget, so they share one window — which
forced a decision about semantics: **a gap is not jitter.** The shared window excludes
inter-arrivals beyond 5× the expected period, which also fixed the reported `jitter_cv`
being inflated on any topic with a dropout.

## Clock steps versus recorder lag

A clock step and a growing recorder lag both show up as `log_time − publish_time`
changing. Distinguishing them matters, because one means "NTP corrected" and the other
means "you are losing data right now".

The detector keeps a step-correction offset: when a step larger than 500 ms is seen, the
offset absorbs it and the lag EWMA continues from the corrected baseline. Steps are
deduplicated within one second, because every topic crosses the same step and reporting
it five times is noise.

Across a clock that ran *backwards*, recorder lag is not measurable at all — the two time
bases interleave and any number would be an artefact of the jump. The detector says that
instead of reporting a fictional curve. This is also why `clock_step` findings are timed
at the **publish** instant: the log clock is the one that moved, so timing the event by it
would place the finding wherever the step displaced it.

## The response budgeter teaches, it does not just truncate

Every result passes through a reduction ladder — raw values, decimated values, binned
stats, summary — and when it truncates it attaches `suggested_narrowing`. Truncation
without guidance just makes an agent retry the same question. The `caveats` field on the
health report is the same idea applied to correctness rather than size: it states what
the recording *cannot* support, which prevents a large class of confident-but-wrong
conclusions.

## Where the plan was wrong

**Throughput.** The design targeted ≥500 MB/s payload-free scanning. Measured: ~64k
messages/s scanning and ~21k messages/s auditing, which on small synthetic messages is
about 1.5 MB/s. The bottleneck is per-message Python overhead and chunk decompression,
not payload parsing, so MB/s scales with payload size while messages/s stays flat. The
bench asserts messages/s, which is the number that actually predicts runtime.

**State budget.** The target was <2 KB per topic. Measured with the accuracy-tuned
defaults: 3,136 B. Rather than quietly redefining the accounting, the defaults stay where
detection is best and `BAGLENS_EDGE_PROFILE=1` shrinks the windows to 1,888 B for the
device case.

**Detection scores.** All eight detectors sit at precision and recall 1.000 with zero
false positives on 40 clean bags. That is a regression gate, not a field claim: the same
repository defines both the fault and the detector, so agreement is partly circular. The
number that would prove field accuracy is a real finding in a recording nobody here made.

## The config proxy

`CONFIG` is a proxy object rather than a module global. Modules do
`from .config import CONFIG` at import time, so rebinding a plain global in `set_config`
would leave every importer holding the old object — which is precisely how `--root`
confinement silently stops applying. One indirection guarantees a single live config.
The same class of bug bit the indexer's `STATUS` and is fixed the same way.
