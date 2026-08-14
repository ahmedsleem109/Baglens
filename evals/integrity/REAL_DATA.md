# Detector performance on real flight data

Corpus: **12 distinct public PX4 flights** from review.px4.io (4 duplicate uploads removed by content hash), 99 minutes of flight. Generated 2026-08-14.

**Labels are not ours.** PX4's logger writes a dropout record whenever it could not keep up. That record is the ground truth here — authored by the flight controller, on hardware we have never touched. Every other precision/recall number in this repository is scored against faults this repository injected.

Matching tolerance ±2s. Dropout marks shorter than 200 ms are ignored.

| Metric | Value |
|---|---|
| Labelled dropouts | 100 |
| Correlation findings | 119 |
| **Recall** | **1.000** |
| **Precision** | **0.832** |
| F1 | 0.908 |
| Recording time lost to logger stalls | 244s of 5920s (4.1%) |

## Per flight

| Flight | Duration | Labelled | Reported | Matched | Stall time |
|---|---|---|---|---|---|
| `368f347b-35b5-4a3a` | 1358s | 42 | 42 | 42 | 97.9s |
| `7078761d-e751-40d9` | 883s | 25 | 24 | 25 | 64.9s |
| `0ca5dafd-f934-4cd7` | 356s | 11 | 15 | 11 | 28.5s |
| `6770739a-e24a-43db` | 344s | 12 | 17 | 12 | 28.1s |
| `588ff157-ed70-4417` | 312s | 8 | 14 | 8 | 21.8s |
| `fddab288-7472-42b4` | 367s | 2 | 3 | 2 | 3.1s |
| `1c100cec-8e0a-49bf` | 716s | 0 | 1 | 0 | 0.0s |
| `7373e4bf-f92d-4b86` | 202s | 0 | 0 | 0 | 0.0s |
| `8cad5898-7610-469c` | 522s | 0 | 2 | 0 | 0.0s |
| `9ebc5e26-8143-47f1` | 333s | 0 | 1 | 0 | 0.0s |
| `a5abe811-7c23-43d4` | 357s | 0 | 0 | 0 | 0.0s |
| `d9ab45ca-127d-4120` | 171s | 0 | 0 | 0 | 0.0s |

## What this measures, and what it does not

It measures whether the correlation detector finds the recorder stalls that the recorder itself admitted to. It does **not** measure the per-topic gap detector, whose findings on this corpus are dominated by the same stalls seen once per topic rather than once per event.

A missed label is not automatically a detector failure: a dropout the logger recorded while only low-rate topics were due can pass without leaving a detectable hole in the arrival stream. The floor on dropout duration exists for the same reason.

