# Detector performance on real flight data

Corpus: **105 distinct public PX4 flights** from review.px4.io (16 duplicate uploads removed by content hash), 677 minutes of flight. Generated 2026-08-16.

**Labels are not ours.** PX4's logger writes a dropout record whenever it could not keep up. That record is the ground truth here — authored by the flight controller, on hardware we have never touched. Every other precision/recall number in this repository is scored against faults this repository injected.

Matching tolerance ±2s. Dropout marks shorter than 200 ms are ignored.

| Metric | Value |
|---|---|
| Labelled dropouts | 152 |
| Correlation findings | 154 |
| **Recall** | **0.993** |
| **Precision** | **0.955** |
| F1 | 0.974 |
| Recording time lost to logger stalls | 307s of 40629s (0.8%) |

## Per flight

| Flight | Duration | Labelled | Reported | Matched | Stall time |
|---|---|---|---|---|---|
| `368f347b-35b5-4a3a` | 1357s | 42 | 41 | 41 | 97.9s |
| `7078761d-e751-40d9` | 882s | 25 | 24 | 25 | 64.9s |
| `0ca5dafd-f934-4cd7` | 355s | 11 | 11 | 11 | 28.5s |
| `6770739a-e24a-43db` | 343s | 12 | 12 | 12 | 28.1s |
| `588ff157-ed70-4417` | 311s | 8 | 8 | 8 | 21.8s |
| `27a80339-a4bc-4b29` | 1294s | 18 | 18 | 18 | 18.1s |
| `15f4074b-d833-480c` | 643s | 8 | 8 | 8 | 14.8s |
| `65719ee5-d269-4cc4` | 160s | 2 | 2 | 2 | 6.4s |
| `4a0cf55a-863e-4cdc` | 1896s | 3 | 2 | 3 | 5.7s |
| `fa18de53-07c2-4266` | 171s | 5 | 4 | 5 | 5.4s |
| `fb5c0315-e984-4191` | 102s | 4 | 3 | 4 | 4.2s |
| `fddab288-7472-42b4` | 367s | 2 | 2 | 2 | 3.1s |
| `3fe8bc6c-1b3b-49e2` | 115s | 1 | 1 | 1 | 2.7s |
| `20a92101-5b3d-4230` | 317s | 1 | 1 | 1 | 1.3s |
| `b080f945-7ad6-4327` | 597s | 2 | 4 | 2 | 0.9s |
| `a23b7923-daf2-4e95` | 156s | 1 | 1 | 1 | 0.8s |
| `325d5559-7420-4314` | 466s | 2 | 2 | 2 | 0.5s |
| `317521cf-2243-423e` | 339s | 1 | 1 | 1 | 0.5s |
| `d87a06a3-a6b0-4e41` | 323s | 1 | 1 | 1 | 0.4s |
| `02b71dc2-9b5b-40a3` | 213s | 1 | 1 | 1 | 0.3s |
| `76528d55-49f7-4174` | 316s | 1 | 1 | 1 | 0.3s |
| `eec72a87-7da6-4f4c` | 148s | 1 | 1 | 1 | 0.3s |
| `00bdb07a-8b8d-4018` | 110s | 0 | 0 | 0 | 0.0s |
| `03432416-1bfd-4e31` | 103s | 0 | 0 | 0 | 0.0s |
| `03edb6ab-5408-4d09` | 102s | 0 | 0 | 0 | 0.0s |
| `04d89640-5e4f-407d` | 1242s | 0 | 0 | 0 | 0.0s |
| `08b1644b-49a9-49ec` | 238s | 0 | 0 | 0 | 0.0s |
| `0996b141-9c2d-4069` | 390s | 0 | 0 | 0 | 0.0s |
| `0d80675b-ad9a-4cfb` | 186s | 0 | 0 | 0 | 0.0s |
| `0dace365-85e4-46a0` | 113s | 0 | 0 | 0 | 0.0s |
| `19aecbdd-0b4c-41af` | 615s | 0 | 0 | 0 | 0.0s |
| `1a336ff4-eb1d-483e` | 227s | 0 | 0 | 0 | 0.0s |
| `1c100cec-8e0a-49bf` | 715s | 0 | 0 | 0 | 0.0s |
| `2010c82f-4320-4580` | 320s | 0 | 0 | 0 | 0.0s |
| `23c528ee-3948-4e34` | 522s | 0 | 0 | 0 | 0.0s |
| `24ee3ab4-ed08-48da` | 393s | 0 | 0 | 0 | 0.0s |
| `25b25a69-eb1d-4e40` | 106s | 0 | 0 | 0 | 0.0s |
| `2894347c-5251-4f67` | 311s | 0 | 0 | 0 | 0.0s |
| `2b13e8f1-2640-4bbf` | 244s | 0 | 0 | 0 | 0.0s |
| `38f7a2db-c61b-4378` | 499s | 0 | 0 | 0 | 0.0s |
| `3a305d7c-e8b1-4ec9` | 472s | 0 | 0 | 0 | 0.0s |
| `3c05b613-30e1-4767` | 400s | 0 | 0 | 0 | 0.0s |
| `3ffac3c9-12e7-4dbf` | 208s | 0 | 0 | 0 | 0.0s |
| `491cef2e-6795-4fee` | 214s | 0 | 0 | 0 | 0.0s |
| `4972c5a5-7e47-4c11` | 179s | 0 | 0 | 0 | 0.0s |
| `4b2f8a60-ff3c-45fa` | 660s | 0 | 0 | 0 | 0.0s |
| `4f2828b9-9763-4d9d` | 295s | 0 | 0 | 0 | 0.0s |
| `52ce1deb-f834-44eb` | 208s | 0 | 0 | 0 | 0.0s |
| `53aabce9-3bb8-431c` | 1680s | 0 | 0 | 0 | 0.0s |
| `5518e9c2-096a-4f76` | 158s | 0 | 0 | 0 | 0.0s |
| `56769f0b-2e02-4a18` | 92s | 0 | 0 | 0 | 0.0s |
| `5deaa5e7-c1ed-4e28` | 616s | 0 | 0 | 0 | 0.0s |
| `5ffe57d7-7037-4a53` | 633s | 0 | 1 | 0 | 0.0s |
| `6295753e-a027-4b6b` | 207s | 0 | 0 | 0 | 0.0s |
| `65bf1ef9-befc-43ac` | 110s | 0 | 0 | 0 | 0.0s |
| `672e6505-d05c-46c9` | 151s | 0 | 0 | 0 | 0.0s |
| `6a19853b-1a50-4c08` | 231s | 0 | 0 | 0 | 0.0s |
| `6ca4b9e6-053b-4252` | 141s | 0 | 1 | 0 | 0.0s |
| `6cbbd17c-63bc-44c9` | 97s | 0 | 0 | 0 | 0.0s |
| `7373e4bf-f92d-4b86` | 202s | 0 | 0 | 0 | 0.0s |
| `7b18658c-59f4-495d` | 249s | 0 | 0 | 0 | 0.0s |
| `7c947147-ba60-4025` | 223s | 0 | 0 | 0 | 0.0s |
| `7dcf419d-5032-49dc` | 702s | 0 | 0 | 0 | 0.0s |
| `7e255d3c-5227-4d78` | 137s | 0 | 0 | 0 | 0.0s |
| `7fb35c43-78ef-418f` | 435s | 0 | 0 | 0 | 0.0s |
| `84960b7b-80a3-447c` | 100s | 0 | 0 | 0 | 0.0s |
| `882ec9b6-9c3b-4d1e` | 149s | 0 | 0 | 0 | 0.0s |
| `9021d747-2f6f-4eac` | 277s | 0 | 0 | 0 | 0.0s |
| `90ef399d-87f0-431b` | 163s | 0 | 0 | 0 | 0.0s |
| `92cbe5ea-45da-4062` | 100s | 0 | 0 | 0 | 0.0s |
| `9c1aff9d-4f52-462e` | 348s | 0 | 0 | 0 | 0.0s |
| `9d02410c-46e8-4130` | 706s | 0 | 0 | 0 | 0.0s |
| `9ebc5e26-8143-47f1` | 332s | 0 | 0 | 0 | 0.0s |
| `a1f88da6-1425-4326` | 1573s | 0 | 0 | 0 | 0.0s |
| `a272e5ff-375e-47f4` | 331s | 0 | 0 | 0 | 0.0s |
| `a2fe7c84-c8e4-4126` | 156s | 0 | 0 | 0 | 0.0s |
| `a5abe811-7c23-43d4` | 357s | 0 | 0 | 0 | 0.0s |
| `a7fa73ea-bb43-4ba8` | 415s | 0 | 0 | 0 | 0.0s |
| `a84201ab-614f-4982` | 178s | 0 | 0 | 0 | 0.0s |
| `ad14a959-9ceb-45e9` | 162s | 0 | 0 | 0 | 0.0s |
| `adfb286c-1ac8-4bb1` | 573s | 0 | 0 | 0 | 0.0s |
| `af4d800e-5e77-4f4e` | 109s | 0 | 0 | 0 | 0.0s |
| `afb54851-ad72-4664` | 270s | 0 | 0 | 0 | 0.0s |
| `b1817cc4-3a6d-4fc9` | 192s | 0 | 0 | 0 | 0.0s |
| `b729c072-c831-42b6` | 177s | 0 | 0 | 0 | 0.0s |
| `b9557df8-e028-4c28` | 782s | 0 | 0 | 0 | 0.0s |
| `c611849c-33e0-4bac` | 361s | 0 | 0 | 0 | 0.0s |
| `cb59d4eb-a747-4a84` | 102s | 0 | 0 | 0 | 0.0s |
| `cbbf1568-0eb0-46c8` | 990s | 0 | 0 | 0 | 0.0s |
| `d4c32e25-df98-4d78` | 1432s | 0 | 0 | 0 | 0.0s |
| `d5b6ca1c-8de8-46c2` | 585s | 0 | 0 | 0 | 0.0s |
| `d661cbf6-a7e3-47db` | 304s | 0 | 0 | 0 | 0.0s |
| `d9ab45ca-127d-4120` | 171s | 0 | 0 | 0 | 0.0s |
| `db8345b5-6ab3-4849` | 252s | 0 | 0 | 0 | 0.0s |
| `dd4e09a2-d05e-46b1` | 231s | 0 | 0 | 0 | 0.0s |
| `de6e8721-43de-4063` | 127s | 0 | 0 | 0 | 0.0s |
| `e3be5c70-5372-4c50` | 428s | 0 | 0 | 0 | 0.0s |
| `e50e5139-90dc-4a01` | 191s | 0 | 0 | 0 | 0.0s |
| `e69587e1-ac46-4bb1` | 169s | 0 | 0 | 0 | 0.0s |
| `ea0769c9-530f-4a6e` | 181s | 0 | 0 | 0 | 0.0s |
| `edf68c41-1621-43fc` | 419s | 0 | 3 | 0 | 0.0s |
| `f027d862-c2f6-4f41` | 398s | 0 | 0 | 0 | 0.0s |
| `f55d6006-e6fc-465b` | 227s | 0 | 0 | 0 | 0.0s |
| `f8a3243c-7847-4ef2` | 194s | 0 | 0 | 0 | 0.0s |
| `f8b0744b-a16e-4295` | 407s | 0 | 0 | 0 | 0.0s |

## What this measures, and what it does not

It measures whether the correlation detector finds the recorder stalls that the recorder itself admitted to. It does **not** measure the per-topic gap detector, whose findings on this corpus are dominated by the same stalls seen once per topic rather than once per event.

A missed label is not automatically a detector failure: a dropout the logger recorded while only low-rate topics were due can pass without leaving a detectable hole in the arrival stream. The floor on dropout duration exists for the same reason.

