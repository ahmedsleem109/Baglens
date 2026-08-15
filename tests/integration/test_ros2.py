"""The ROS 2 subscription source, driven by a stub graph.

There is no ROS installation on the machine that runs these tests, and waiting for one
would mean the source ships untested. What matters is testable without a graph: that the
feed declares the topic count the correlation detector needs, that arrivals reach the
auditor unchanged, and that a monitor which falls behind says so instead of quietly
discarding messages.

The stub implements only what `Ros2Feed` actually calls, so if the feed starts using more
of `rclpy` these tests fail rather than pass by accident.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from baglens.detectors.auditor import Auditor
from baglens.live import LiveMonitor
from baglens.readers import open_bag


class _Clock:
    """Returns the timestamp of the arrival being delivered, so live and offline align."""

    def __init__(self) -> None:
        self.value = 0

    def now(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(nanoseconds=self.value)


class _Node:
    def __init__(self, name: str, topics: list[tuple[str, list[str]]]) -> None:
        self._name = name
        self._topics = topics
        self.clock = _Clock()
        self.callbacks: dict[str, object] = {}

    def get_name(self) -> str:
        return self._name

    def get_topic_names_and_types(self) -> list[tuple[str, list[str]]]:
        return list(self._topics)

    def get_clock(self) -> _Clock:
        return self.clock

    def create_subscription(self, msg_type, topic, callback, qos, raw=False):  # noqa: ANN001
        assert raw, "the monitor must subscribe raw; deserialising payloads is not its job"
        self.callbacks[topic] = callback
        return types.SimpleNamespace(topic=topic)

    def destroy_node(self) -> None:
        return None


@pytest.fixture
def ros_graph(monkeypatch: pytest.MonkeyPatch):
    """Install a fake `rclpy` for the duration of one test, and hand back the node."""
    created: list[_Node] = []
    topics: list[tuple[str, list[str]]] = []

    def create_node(name: str) -> _Node:
        node = _Node(name, topics)
        created.append(node)
        return node

    rclpy = types.ModuleType("rclpy")
    rclpy.ok = lambda: True  # type: ignore[attr-defined]
    rclpy.init = lambda: None  # type: ignore[attr-defined]
    rclpy.create_node = create_node  # type: ignore[attr-defined]

    class _Executor:
        def add_node(self, node): ...
        def spin_once(self, timeout_sec=0.0): ...
        def shutdown(self): ...

    executors = types.ModuleType("rclpy.executors")
    executors.SingleThreadedExecutor = _Executor  # type: ignore[attr-defined]

    qos = types.ModuleType("rclpy.qos")
    for name in ("QoSDurabilityPolicy", "QoSHistoryPolicy", "QoSReliabilityPolicy"):
        setattr(qos, name, types.SimpleNamespace(KEEP_LAST=1, BEST_EFFORT=2, VOLATILE=3))
    qos.QoSProfile = lambda **kw: kw  # type: ignore[attr-defined]

    utilities = types.ModuleType("rosidl_runtime_py.utilities")
    utilities.get_message = lambda type_name: type_name  # type: ignore[attr-defined]
    runtime = types.ModuleType("rosidl_runtime_py")
    runtime.utilities = utilities  # type: ignore[attr-defined]

    for name, module in (
        ("rclpy", rclpy), ("rclpy.executors", executors), ("rclpy.qos", qos),
        ("rosidl_runtime_py", runtime), ("rosidl_runtime_py.utilities", utilities),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    return types.SimpleNamespace(topics=topics, nodes=created)


def _publish_bag(node: _Node, path: Path) -> int:
    """Deliver every arrival in a recording through the stub subscriptions."""
    n = 0
    for arrival in open_bag(path).arrivals():
        callback = node.callbacks.get(arrival.topic)
        if callback is None:
            continue
        node.clock.value = arrival.log_time_ns
        callback(b"\x00" * arrival.size_bytes)
        n += 1
    return n


def test_declares_the_graphs_topic_count(ros_graph, clean_bag: Path) -> None:
    """The correlation denominator floor is the whole reason the feed is a reader.

    A graph whose nodes start over the first ten seconds looks like a system-wide stall
    if concurrency is scored against only the topics seen so far. Offline this is solved
    by the recording's topic table; live, `get_topic_names_and_types()` is that table.
    """
    from baglens.ros2 import Ros2Feed

    meta = open_bag(clean_bag).metadata()
    ros_graph.topics.extend((t.topic, ["std_msgs/msg/String"]) for t in meta.topics)

    with Ros2Feed() as feed:
        auditor = Auditor(feed.reader)
        auditor._ensure_global_detectors()
        assert auditor._expected_topic_count() == len(meta.topics)
        assert auditor.correlation.expected_topics == len(meta.topics)


def test_subscription_arrivals_match_an_offline_audit(ros_graph, stall_bag: Path) -> None:
    """Same arrivals, same verdict — the claim the whole live path rests on.

    Fed through subscriptions rather than a file, with the node's clock reporting each
    arrival's own timestamp, the report must be the one the file produces. A difference
    here means the source is editing the stream on its way past.
    """
    from baglens.ros2 import Ros2Feed

    meta = open_bag(stall_bag).metadata()
    ros_graph.topics.extend((t.topic, ["std_msgs/msg/String"]) for t in meta.topics)

    feed = Ros2Feed(queue_size=1_000_000)
    feed.start()
    node = ros_graph.nodes[0]
    published = _publish_bag(node, stall_bag)
    assert published > 0
    feed.idle_timeout_s = 0.05  # the graph has gone quiet; end the feed

    monitor = LiveMonitor(feed)
    live = list(monitor.run())[-1]
    feed.stop()

    offline = Auditor(open_bag(stall_bag)).run()
    assert feed.dropped == 0
    assert live.verdict == offline.verdict
    assert round(live.overall_score, 6) == round(offline.overall_score, 6)
    assert not [f for f in live.findings if f.detector == "file_integrity"], (
        "a subscription has no file; integrity findings about one are noise"
    )
    assert sorted((f.detector, round(f.t_start, 6), round(f.t_end, 6), f.summary)
                  for f in live.findings) == sorted(
        (f.detector, round(f.t_start, 6), round(f.t_end, 6), f.summary)
        for f in offline.findings
    )


def test_a_monitor_that_falls_behind_counts_what_it_lost(ros_graph, clean_bag: Path) -> None:
    """A full queue means the monitor is behind. That is a number, not a silence.

    Reporting improving health because arrivals were dropped on the floor is the single
    most dangerous failure this component can have — it would look healthiest exactly
    when the vehicle is worst.
    """
    from baglens.ros2 import Ros2Feed

    meta = open_bag(clean_bag).metadata()
    ros_graph.topics.extend((t.topic, ["std_msgs/msg/String"]) for t in meta.topics)

    feed = Ros2Feed(queue_size=64)
    feed.start()
    published = _publish_bag(ros_graph.nodes[0], clean_bag)
    feed.stop()

    assert published > 64
    assert feed.dropped == published - 64
