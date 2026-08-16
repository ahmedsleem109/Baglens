"""ROS 2 subscription source — the same detectors, fed by a live graph.

This is the last genuinely missing piece for on-vehicle monitoring, and it is small on
purpose. `test_live.py` already proves that the detectors do not care where arrivals come
from: offline and live produce byte-identical verdicts on real flights. So this file adds
a feed and nothing else — no detector, no threshold, no second code path.

Three things it must get right, all of which are about honesty rather than throughput:

* **Subscribe raw.** Integrity analysis needs timing and size, never payload. Raw
  subscriptions skip deserialisation entirely, which is both faster and the reason a
  camera topic costs the same as an odometry topic here.
* **Declare the topic count.** The correlation detector floors its denominator on the
  topics the source *declares*, not the ones it has seen — without that, a graph whose
  nodes start over ten seconds reads as a system-wide stall. `get_topic_names_and_types()`
  is the live equivalent of a recording's topic table.
* **Never drop silently.** A bounded queue is the only safe way to cross from executor
  threads to the audit loop, and a full queue means the monitor is behind. That is
  reported as a number, not swallowed: a monitor that quietly discards arrivals would
  report improving health exactly when the vehicle is struggling.

`rclpy` is imported lazily so that importing `baglens` on a machine with no ROS
installation stays free.

    from baglens.live import LiveMonitor
    from baglens.ros2 import Ros2Feed

    with Ros2Feed() as feed:
        for report in LiveMonitor(feed).run(snapshot_every_s=5.0):
            print(report.verdict, len(report.findings))
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .readers.base import Arrival, BagMetadata, TopicInfo
from .readers.stamp_peek import peek_stamp_ns, stamp_offset_for_type

#: how often to re-scan the graph for topics that appeared after startup
DISCOVERY_INTERVAL_S = 2.0

#: arrivals buffered between the executor thread and the audit loop. At 25k msg/s — the
#: measured Python ceiling — this is about a third of a second of slack, enough to ride
#: out a snapshot without becoming a hidden second buffer that hides real backpressure.
QUEUE_SIZE = 8192


class Ros2GraphReader:
    """A `BagReader` view of a live ROS 2 graph.

    The auditor asks its reader for metadata once, at construction, and uses the topic
    table to size the correlation denominator. A live graph can answer that question —
    `get_topic_names_and_types()` is exactly the recording's topic table — so the feed
    presents itself as a reader rather than making the auditor special-case liveness.

    Everything that needs payload (`messages`, `numeric_field`) is empty here by design.
    Those are the offline investigation tools; a subscription has no history to search.
    """

    format = "ros2"
    #: there is no file behind a subscription, so the file-integrity checks are skipped
    #: rather than answered with "does not exist"
    has_file = False

    def __init__(self, node: Any, topics: list[tuple[str, str]]) -> None:
        self.path = Path("ros2://" + (node.get_name() if node else "graph"))
        self._topics = topics
        self._started_ns = time.time_ns()

    def metadata(self) -> BagMetadata:
        meta = BagMetadata(path=str(self.path), format="ros2", start_time_ns=self._started_ns)
        # count stays 0: nothing has arrived when the auditor asks, and that is what makes
        # `_expected_topic_count` fall back to the declared list — the behaviour we want
        meta.topics = [TopicInfo(topic=t, msg_type=ty) for t, ty in self._topics]
        meta.in_progress = True
        meta.has_summary = False
        meta.warnings.append(
            "live ROS 2 subscription: arrival time is the subscriber's clock, so recorder "
            "lag (D6b) is not measurable and messages lost before this process started "
            "cannot be seen"
        )
        return meta

    def arrivals(
        self, topics: list[str] | None = None, start_time_ns: int | None = None
    ) -> Iterator[Arrival]:
        return iter(())  # the feed is the arrival source; the reader is only the topic table

    def numeric_field(self, topic: str, path: str) -> Iterator[tuple[int, float]]:
        return iter(())

    def messages(
        self, topics: list[str] | None = None,
        start_s: float | None = None, end_s: float | None = None,
    ) -> Iterator[tuple[str, int, Any]]:
        return iter(())

    def schema_text(self, topic: str) -> str:
        return next((ty for t, ty in self._topics if t == topic), "")

    def close(self) -> None:
        return None


class Ros2Feed:
    """Subscribe to a live graph and yield arrivals as they land.

    `topics` restricts the subscription; the default is everything the graph advertises,
    which is what an integrity monitor wants — the topic that went silent is rarely the
    one you thought to name.
    """

    def __init__(
        self,
        topics: list[str] | None = None,
        node_name: str = "baglens_monitor",
        queue_size: int = QUEUE_SIZE,
        idle_timeout_s: float = 0.0,
        discovery_interval_s: float = DISCOVERY_INTERVAL_S,
        want_stamps: bool = True,
    ) -> None:
        self.topic_filter = topics
        #: peek `header.stamp` off each raw buffer, so the live path can measure data age
        #: exactly as the offline one does. On by default: a live monitor that cannot
        #: answer "how old is this data?" is the one place the question is urgent.
        self.want_stamps = want_stamps
        self.node_name = node_name
        self.idle_timeout_s = idle_timeout_s
        self.discovery_interval_s = discovery_interval_s
        #: arrivals the queue could not accept — the monitor falling behind, reported
        #: rather than hidden
        self.dropped = 0
        self._q: queue.Queue[Arrival] = queue.Queue(maxsize=queue_size)
        self._subscribed: set[str] = set()
        self._node: Any = None
        self._executor: Any = None
        self._spin: threading.Thread | None = None
        self._stop = threading.Event()
        self._reader: Ros2GraphReader | None = None

    # -- lifecycle ---------------------------------------------------------

    def _rclpy(self) -> Any:
        try:
            import rclpy
        except ImportError as exc:  # pragma: no cover - depends on a ROS installation
            raise ImportError(
                "live ROS 2 monitoring requires rclpy — source a ROS 2 environment "
                "(e.g. `source /opt/ros/jazzy/setup.bash`) before running"
            ) from exc
        return rclpy

    def start(self) -> None:
        if self._node is not None:
            return
        rclpy = self._rclpy()
        if not rclpy.ok():
            rclpy.init()
        self._node = rclpy.create_node(self.node_name)
        self._reader = Ros2GraphReader(self._node, self._discover())
        self._subscribe_new()

        from rclpy.executors import SingleThreadedExecutor

        self._executor = SingleThreadedExecutor()
        self._executor.add_node(self._node)
        self._spin = threading.Thread(target=self._spin_forever, name="baglens-rclpy",
                                      daemon=True)
        self._spin.start()

    def _spin_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self._executor.spin_once(timeout_sec=0.1)
            except Exception:  # pragma: no cover - shutdown races
                return

    def stop(self) -> None:
        self._stop.set()
        if self._spin is not None:
            self._spin.join(timeout=2.0)
        if self._executor is not None:
            self._executor.shutdown()
        if self._node is not None:
            self._node.destroy_node()
        self._node = self._executor = self._spin = None

    def __enter__(self) -> Ros2Feed:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- subscription ------------------------------------------------------

    def _discover(self) -> list[tuple[str, str]]:
        found = self._node.get_topic_names_and_types()
        wanted = set(self.topic_filter) if self.topic_filter else None
        return [
            (topic, types[0] if types else "")
            for topic, types in sorted(found)
            if wanted is None or topic in wanted
        ]

    def _subscribe_new(self) -> int:
        """Subscribe to any topic not already covered. Returns how many were added.

        Re-run periodically because a graph is not static: nodes come up over the first
        seconds of a mission, and a monitor that only ever sees the topics present at
        startup would miss the ones that matter most.
        """
        from rclpy.qos import (
            QoSDurabilityPolicy,
            QoSHistoryPolicy,
            QoSProfile,
            QoSReliabilityPolicy,
        )
        from rosidl_runtime_py.utilities import get_message

        # Best-effort with a shallow queue: an integrity monitor must never apply
        # backpressure to the system it is watching, and a reliable subscription would.
        qos = QoSProfile(
            depth=10,
            history=QoSHistoryPolicy.KEEP_LAST,
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            durability=QoSDurabilityPolicy.VOLATILE,
        )
        added = 0
        for topic, type_name in self._discover():
            if topic in self._subscribed or not type_name:
                continue
            try:
                msg_type = get_message(type_name)
            except Exception:  # a type this machine has no definition for
                continue
            offset = stamp_offset_for_type(msg_type) if self.want_stamps else None
            self._node.create_subscription(
                msg_type, topic, self._make_callback(topic, offset), qos, raw=True
            )
            self._subscribed.add(topic)
            added += 1
        return added

    def _make_callback(self, topic: str, stamp_at: int | None = None) -> Any:
        clock = self._node.get_clock()

        def on_message(raw: bytes) -> None:
            # Raw subscription: `raw` is the serialised buffer, so this costs a length
            # check rather than a deserialisation. `stamp_at` is set only for topics whose
            # type declares a leading header and only when stamps were asked for, and it
            # buys one 8-byte peek — the same one the file path takes, deliberately from
            # the same module, so live and offline cannot drift apart on the same bytes.
            now = clock.now().nanoseconds
            stamp = peek_stamp_ns(raw, stamp_at) if stamp_at is not None else None
            try:
                self._q.put_nowait(Arrival(topic, now, now, len(raw), stamp))
            except queue.Full:
                self.dropped += 1

        return on_message

    # -- the feed ----------------------------------------------------------

    @property
    def reader(self) -> Ros2GraphReader:
        if self._reader is None:
            self.start()
        assert self._reader is not None
        return self._reader

    def arrivals(self) -> Iterator[Arrival]:
        self.start()
        last_discovery = last_message = time.monotonic()
        while True:
            try:
                yield self._q.get(timeout=0.1)
                last_message = time.monotonic()
            except queue.Empty:
                if self.idle_timeout_s and time.monotonic() - last_message > self.idle_timeout_s:
                    return
            now = time.monotonic()
            if now - last_discovery >= self.discovery_interval_s:
                last_discovery = now
                self._subscribe_new()
