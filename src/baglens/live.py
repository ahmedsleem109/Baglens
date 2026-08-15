"""Live monitoring: the same detectors, fed by a clock instead of a file.

The whole library was built single-pass with bounded state so that this file could be
small. Nothing here reimplements a detector, changes a threshold, or special-cases the
live path — a `LiveMonitor` is an `Auditor` with three things added:

* **a feed** that delivers arrivals as they happen rather than as fast as the disk can
  read them,
* **snapshots**, so you can ask "what is the verdict *so far*" without ending the run,
* **checkpoints**, so a monitor that is restarted resumes with its learned baselines
  instead of spending its warmup window blind all over again.

The snapshot is the interesting one. Rather than calling `finish()` on the live auditor —
which closes detectors and is not something you can undo — a snapshot round-trips the
auditor through `to_state()` / `from_state()` and finishes the *copy*. The live auditor is
never touched, and every snapshot exercises the checkpoint path, so a serialisation bug
shows up in the first minute of monitoring rather than the first crash recovery.

Two feeds ship here:

* `ReplayFeed` — a recorded file, paced to the wall clock. This is how the live path is
  tested against *real* flights rather than synthetic ones: the arrival stream is real
  PX4 data, only the delivery is simulated.
* `TailFeed` — a file still being written, polled for growth. This is the honest first
  step to on-vehicle monitoring: it needs no ROS installation and exercises the same
  code path a subscription would.

A ROS 2 subscription feed is deliberately not here: it would need `rclpy` and a running
graph, and it would add nothing this file does not already prove.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .config import CONFIG, Config
from .detectors.auditor import Auditor
from .models import HealthReport
from .readers import Arrival, BagReader, open_bag


class Feed(Protocol):
    """Anything that produces arrivals over time.

    A file reader is already almost this; the difference is that a feed is allowed to
    block, and is allowed never to end.
    """

    def arrivals(self) -> Iterator[Arrival]: ...

    @property
    def reader(self) -> BagReader: ...


@dataclass
class ReplayFeed:
    """Replay a recording at wall-clock speed — real data, simulated delivery.

    `speed` multiplies the rate: 1.0 is real time, 60.0 replays a minute per second, and
    0 means "as fast as possible", which is useful for the equivalence test below where
    only ordering matters, not timing.

    Sleeping per arrival would be both inaccurate and slow at PX4's ~2.7 kHz aggregate
    rate, so the feed sleeps only when it has drifted more than `tick_s` behind the
    schedule. That keeps the pacing honest at the granularity anyone can observe while
    letting bursts through at full speed.
    """

    path: str | Path
    speed: float = 1.0
    tick_s: float = 0.05
    topics: list[str] | None = None

    _reader: BagReader | None = field(default=None, init=False, repr=False)

    @property
    def reader(self) -> BagReader:
        if self._reader is None:
            self._reader = open_bag(self.path)
        return self._reader

    def arrivals(self) -> Iterator[Arrival]:
        started = time.monotonic()
        t0_ns: int | None = None
        for arrival in self.reader.arrivals(self.topics):
            if t0_ns is None:
                t0_ns = arrival.log_time_ns
            if self.speed > 0:
                target = (arrival.log_time_ns - t0_ns) / 1e9 / self.speed
                behind = target - (time.monotonic() - started)
                if behind > self.tick_s:
                    time.sleep(behind)
            yield arrival


@dataclass
class TailFeed:
    """Follow a recording that is still being written.

    Re-opens the file whenever it grows and resumes after the arrivals already seen.
    Crude by design: it needs no inotify (unreliable across `/mnt/c`) and no ROS, and it
    is enough to prove the detectors behave on a stream that has no end. `idle_timeout_s`
    ends the feed after a stretch with no growth, so a caller is not stuck forever on a
    recording whose writer has died.
    """

    path: str | Path
    poll_s: float = 0.5
    idle_timeout_s: float = 30.0
    topics: list[str] | None = None

    _reader: BagReader | None = field(default=None, init=False, repr=False)

    @property
    def reader(self) -> BagReader:
        if self._reader is None:
            self._reader = open_bag(self.path)
        return self._reader

    def arrivals(self) -> Iterator[Arrival]:
        seen = 0
        last_growth = time.monotonic()
        last_size = -1
        while True:
            size = Path(self.path).stat().st_size
            if size != last_size:
                last_size, last_growth = size, time.monotonic()
                fresh = open_bag(self.path)
                self._reader = fresh
                for i, arrival in enumerate(fresh.arrivals(self.topics)):
                    if i < seen:
                        continue
                    seen = i + 1
                    yield arrival
            if time.monotonic() - last_growth > self.idle_timeout_s:
                return
            time.sleep(self.poll_s)


class LiveMonitor:
    """Run the detector library against a feed, with snapshots and checkpoints.

    Usage is deliberately boring::

        monitor = LiveMonitor(ReplayFeed(path, speed=60))
        for report in monitor.run(snapshot_every_s=1.0):
            print(report.verdict, len(report.findings))

    Each yielded report is a complete `HealthReport` for everything seen so far.
    """

    def __init__(
        self,
        feed: Feed,
        cfg: Config | None = None,
        detectors: list[str] | None = None,
        checkpoint_path: str | Path | None = None,
    ) -> None:
        self.feed = feed
        self.cfg = cfg or CONFIG
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
        self.auditor = self._restore_or_new(detectors)
        self.n = 0

    def _restore_or_new(self, detectors: list[str] | None) -> Auditor:
        if self.checkpoint_path and self.checkpoint_path.exists():
            state = json.loads(self.checkpoint_path.read_text())
            return Auditor.from_state(state, self.feed.reader, self.cfg)
        return Auditor(self.feed.reader, self.cfg, detectors=detectors)

    def snapshot(self) -> HealthReport:
        """The verdict so far, without ending the run.

        Finishing a *copy* rather than the live auditor is what makes this safe to call
        as often as you like: `finalize()` closes open gaps and is not designed to be
        called twice, and a live monitor that corrupted its own state every time someone
        asked for a status would be worse than no status at all.
        """
        state = self.auditor.to_state()
        twin = Auditor.from_state(state, self.feed.reader, self.cfg)
        return twin.finish()

    def checkpoint(self) -> None:
        if self.checkpoint_path is None:
            return
        tmp = self.checkpoint_path.with_suffix(self.checkpoint_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.auditor.to_state()))
        tmp.replace(self.checkpoint_path)  # atomic: a torn checkpoint is worse than none

    def run(
        self,
        snapshot_every_s: float = 0.0,
        checkpoint_every_s: float = 0.0,
        snapshot_every_n: int = 0,
        on_arrival: Callable[[Arrival], None] | None = None,
    ) -> Iterator[HealthReport]:
        """Consume the feed, yielding a snapshot on the requested cadence.

        `snapshot_every_s` paces by wall clock, which is what an operator display wants.
        `snapshot_every_n` paces by arrival count, which is what a test wants: it is
        deterministic, and it does not depend on how fast the machine happens to be.

        Both default to 0, meaning "no interim snapshots" — that case yields exactly one
        report at the end and is equivalent to `Auditor.run()`.

        A snapshot is not free: it serialises and restores the whole auditor. At PX4's
        ~2.7 kHz aggregate rate, asking for one per arrival means thousands of full
        round-trips and the run will appear to hang. Pace it in seconds for a display, or
        in thousands of arrivals for a test.
        """
        self.auditor._ensure_global_detectors()
        last_snap = last_ckpt = time.monotonic()
        since_snap = 0
        for arrival in self.feed.arrivals():
            self.auditor.push(arrival)
            self.n += 1
            since_snap += 1
            if on_arrival is not None:
                on_arrival(arrival)
            now = time.monotonic()
            due = snapshot_every_s and now - last_snap >= snapshot_every_s
            if snapshot_every_n and since_snap >= snapshot_every_n:
                due = True
            if due:
                last_snap, since_snap = now, 0
                yield self.snapshot()
            if checkpoint_every_s and now - last_ckpt >= checkpoint_every_s:
                last_ckpt = now
                self.checkpoint()
        self.checkpoint()
        yield self.auditor.finish()


def audit_live(feed: Feed, cfg: Config | None = None) -> HealthReport:
    """Run a feed to completion and return the final report.

    `run()` always yields a final report last, so the deque of size 1 is guaranteed
    non-empty for any feed that terminates.
    """
    monitor = LiveMonitor(feed, cfg)
    last = deque(monitor.run(), maxlen=1)
    return last[0]
