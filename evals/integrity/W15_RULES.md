# W15 — should D7 honour `unassessable`?

Three rules, two labelled corpora, one table. The question is whether a topic with no usable rate model may open a silent interval, and whether it may count as co-silent inside someone else's.

PX4: all flights, scored against the logger's own dropout records. Injected: real ROS 2 recordings with exact labels — see `INJECTED.md`.

| D7 rule | PX4 recall | PX4 precision | Injected recall | Injected precision | `nuway_stops` verdict | claimed stall |
|---|---|---|---|---|---|---|
| unrestricted (shipped) | 0.993 | 0.942 | 0.824 | 1.000 | compromised at 0.0 | 627s |
| aperiodic may not create; anyone may vote | 0.993 | 0.942 | 0.824 | 1.000 | trustworthy at 98.8 | 7s |
| aperiodic may not create or vote | 0.993 | 0.955 | 0.824 | 1.000 | trustworthy at 98.7 | 8s |

Regenerate: `uv run python scripts/w15_rules.py --px4 /home/sleem/data/public/px4 --injected /home/sleem/data/injected`.

