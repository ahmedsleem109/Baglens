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


def redact(report: HealthReport) -> HealthReport:
    """Drop redacted topics before anything leaves the tool boundary."""
    if not CONFIG.redact_topics:
        return report
    blocked = set(CONFIG.redact_topics)

    def hidden(topic: str | None) -> bool:
        return topic is not None and any(
            topic == b or (b.endswith("*") and topic.startswith(b[:-1])) for b in blocked
        )

    report.topics = [t for t in report.topics if not hidden(t.topic)]
    report.findings = [f for f in report.findings if not hidden(f.topic)]
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
