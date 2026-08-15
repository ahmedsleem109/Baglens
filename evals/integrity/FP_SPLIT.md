# P1.1 — the 239 unmatched findings, split by class

Corpus: **105 distinct public PX4 flights**, 677 minutes, 152 labelled dropouts. Matching tolerance ±2s; dropout marks under 200 ms ignored. Generated 2026-08-15.

The published precision scores every `correlation` finding against the ULog dropout labels. But `correlation` emits two different claims, and only one of them is a claim about the recorder:

| Class | Claim | Is a dropout label evidence for it? |
|---|---|---|
| `system-wide stall` | the recorder, disk, CPU or power stopped | yes — this is the same event |
| `subsystem failure` | a shared driver or bus died | no — the label is silent either way |

## Scored separately against the same labels

| Class | Findings | Matched | Precision | Labels found | Recall |
|---|---|---|---|---|---|
| `system-wide stall` only | 147 | 144 | 0.980 | 148/152 | 0.974 |
| `subsystem failure` only | 35 | 28 | 0.800 | 31/152 | 0.204 |
| **both (what is published today)** | 156 | 147 | 0.942 | 151/152 | 0.993 |

The two classes do not sum to the combined row: 26 predicted intervals are produced by merging a subsystem finding into an overlapping stall, so the published denominator is not a count of either claim.

## Where the unmatched findings concentrate

| Flight | Duration | Labelled | Stall | ✗ | Subsystem | ✗ |
|---|---|---|---|---|---|---|
| `b080f945-7ad6-4327` | 597s | 2 | 4 | 2 | 2 | 1 |
| `edf68c41-1621-43fc` | 419s | 0 | 1 | 1 | 2 | 2 |
| `5ffe57d7-7037-4a53` | 633s | 0 | 0 | 0 | 1 | 1 |
| `6ca4b9e6-053b-4252` | 141s | 0 | 0 | 0 | 1 | 1 |
| `a2fe7c84-c8e4-4126` | 156s | 0 | 0 | 0 | 1 | 1 |
| `b729c072-c831-42b6` | 177s | 0 | 0 | 0 | 1 | 1 |
| `00bdb07a-8b8d-4018` | 110s | 0 | 0 | 0 | 0 | 0 |
| `02b71dc2-9b5b-40a3` | 213s | 1 | 0 | 0 | 1 | 0 |
| `03432416-1bfd-4e31` | 103s | 0 | 0 | 0 | 0 | 0 |
| `03edb6ab-5408-4d09` | 102s | 0 | 0 | 0 | 0 | 0 |
| `04d89640-5e4f-407d` | 1242s | 0 | 0 | 0 | 0 | 0 |
| `08b1644b-49a9-49ec` | 238s | 0 | 0 | 0 | 0 | 0 |
| `0996b141-9c2d-4069` | 390s | 0 | 0 | 0 | 0 | 0 |
| `0ca5dafd-f934-4cd7` | 355s | 11 | 11 | 0 | 1 | 0 |
| `0d80675b-ad9a-4cfb` | 186s | 0 | 0 | 0 | 0 | 0 |
| `0dace365-85e4-46a0` | 113s | 0 | 0 | 0 | 0 | 0 |
| `15f4074b-d833-480c` | 643s | 8 | 8 | 0 | 1 | 0 |
| `19aecbdd-0b4c-41af` | 615s | 0 | 0 | 0 | 0 | 0 |
| `1a336ff4-eb1d-483e` | 227s | 0 | 0 | 0 | 0 | 0 |
| `1c100cec-8e0a-49bf` | 715s | 0 | 0 | 0 | 0 | 0 |
| `2010c82f-4320-4580` | 320s | 0 | 0 | 0 | 0 | 0 |
| `20a92101-5b3d-4230` | 317s | 1 | 1 | 0 | 1 | 0 |
| `23c528ee-3948-4e34` | 522s | 0 | 0 | 0 | 0 | 0 |
| `24ee3ab4-ed08-48da` | 393s | 0 | 0 | 0 | 0 | 0 |
| `25b25a69-eb1d-4e40` | 106s | 0 | 0 | 0 | 0 | 0 |

83 of 105 flights carry no dropout label at all and account for 7 of the 9 unmatched findings (78%). On those flights every finding is unmatched by construction, so they set precision without the labels ever being able to confirm one.

