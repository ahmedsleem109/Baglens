# Detector precision and recall

Corpus: **160 faulted** + **40 clean** synthetic bags, 120s each, seed-deterministic.
Matching tolerance: ±2s. Generated 2026-08-16.

| Detector | Precision | Recall | F1 | target P/R | FP per clean bag | verdict |
|---|---|---|---|---|---|---|
| `gap` | 1.000 | 1.000 | 1.000 | 0.90/0.95 | 0.000 | PASS |
| `rate_degradation` | 1.000 | 1.000 | 1.000 | 0.80/0.85 | 0.000 | PASS |
| `jitter` | 1.000 | 1.000 | 1.000 | 0.85/0.80 | 0.000 | PASS |
| `dropped` | 1.000 | 1.000 | 1.000 | 0.80/0.85 | 0.000 | PASS |
| `clock_lag` | 1.000 | 1.000 | 1.000 | 0.85/0.90 | 0.000 | PASS |
| `clock_step` | 1.000 | 1.000 | 1.000 | 0.90/0.95 | 0.000 | PASS |
| `correlation` | 1.000 | 1.000 | 1.000 | 0.80/0.85 | 0.000 | PASS |
| `file_integrity` | 1.000 | 1.000 | 1.000 | 0.95/0.95 | 0.000 | PASS |

Throughput: 1 MB/s across 200 bags (368 MB in 279.6s of audit time, 5 workers).

**How to read this.** A fault counts as detected if the right detector fires with an overlapping window on the right topic. Findings that a different injected fault legitimately explains are not counted as false positives — a 12-second dropout really does drop messages, and penalising the auditor for saying so would measure the wrong thing. Findings on the clean control set are counted with no such leniency.
