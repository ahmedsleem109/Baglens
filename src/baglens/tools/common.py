"""Shared tool plumbing: path resolution, the audit cache, redaction."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any

from ..config import CONFIG
from ..models import Finding, HealthReport


class LruCache:
    """Small in-process cache so drill-down tools do not re-audit the file."""

    def __init__(self, maxsize: int = 8) -> None:
        self.maxsize = maxsize
        self._data: OrderedDict[str, Any] = OrderedDict()

    def get(self, key: str) -> Any | None:
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def values(self) -> list[Any]:
        return list(self._data.values())


AUDIT_CACHE = LruCache(8)


def resolve(path: str) -> Path:
    """Root-confined path resolution. Traversal outside the configured roots is refused."""
    return CONFIG.resolve(path)


def cache_key(path: str | Path, detectors: tuple[str, ...] | None, sensitivity: str) -> str:
    return f"{Path(path).resolve()}|{','.join(detectors or ())}|{sensitivity}"


REDACTED = "<redacted>"


def _topic_matches(pattern: str, topic: str) -> bool:
    return topic == pattern or (pattern.endswith("*") and topic.startswith(pattern[:-1]))


def _field_rules() -> list[tuple[str | None, str]]:
    """Parse BAGLENS_REDACT_FIELDS entries: "field.path" or "/topic:field.path"."""
    rules: list[tuple[str | None, str]] = []
    for entry in CONFIG.redact_fields:
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            topic, _, field_path = entry.partition(":")
            rules.append((topic, field_path))
        else:
            rules.append((None, entry))
    return rules


def is_redacted_topic(topic: str | None) -> bool:
    return topic is not None and any(
        _topic_matches(pattern, topic) for pattern in CONFIG.redact_topics
    )


def is_redacted_field(topic: str, field_path: str) -> bool:
    """True if this dotted field path is masked for this topic.

    A rule matches any contiguous run of path components, so `position` covers both
    `pose.pose.position` and `pose.pose.position.x`, and `latitude` covers `fix.latitude`.
    Matching only suffixes would mask a payload field while still letting the same value
    through `field_stats` one component deeper.
    """
    parts = [p for p in field_path.split(".") if p]
    suffixes = {
        ".".join(parts[i:j])
        for i in range(len(parts))
        for j in range(i + 1, len(parts) + 1)
    } | {field_path}
    for rule_topic, rule_field in _field_rules():
        if rule_topic is not None and not _topic_matches(rule_topic, topic):
            continue
        if rule_field in suffixes:
            return True
    return False


def mask_payload(topic: str, value: Any, prefix: str = "") -> Any:
    """Replace redacted fields inside a decoded payload before it leaves the tool.

    Applied at the tool boundary rather than at the reader, so the detectors keep
    working on complete data while the model never sees the masked values.
    """
    if not CONFIG.redact_fields:
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            out[key] = REDACTED if is_redacted_field(topic, path) else mask_payload(
                topic, item, path
            )
        return out
    if isinstance(value, list):
        return [mask_payload(topic, item, prefix) for item in value]
    return value


def redact(report: HealthReport) -> HealthReport:
    """Drop redacted topics before anything leaves the tool boundary."""
    if not CONFIG.redact_topics:
        return report
    report.topics = [t for t in report.topics if not is_redacted_topic(t.topic)]
    report.findings = [f for f in report.findings if not is_redacted_topic(f.topic)]
    return report


def audit(
    path: str,
    topics: list[str] | None = None,
    detectors: list[str] | None = None,
    sensitivity: str | None = None,
) -> tuple[HealthReport, Any]:
    """Run (or reuse) a single-pass audit. Returns the report and the auditor."""
    from ..detectors import Auditor
    from ..readers import open_bag

    p = resolve(path)
    sens = sensitivity or CONFIG.sensitivity
    key = cache_key(p, tuple(detectors) if detectors else None, sens)
    cached = AUDIT_CACHE.get(key)
    if cached is not None:
        return cached

    cfg = CONFIG.current
    if sens != CONFIG.sensitivity:
        from dataclasses import replace

        cfg = replace(CONFIG.current, sensitivity=sens)  # type: ignore[arg-type]

    reader = open_bag(p)
    auditor = Auditor(reader, cfg=cfg, detectors=detectors, topics=topics)
    report = redact(auditor.run())
    AUDIT_CACHE.put(key, (report, auditor))
    return report, auditor


def find_finding(finding_id: str) -> tuple[Finding, HealthReport] | None:
    for report, _auditor in AUDIT_CACHE.values():
        for f in report.findings:
            if f.id == finding_id:
                return f, report
    return None
