#!/usr/bin/env python3
"""Fetch a corpus of real PX4 flight logs from review.px4.io.

Why this exists: every published precision/recall number in this repository comes
from a generator in this repository, so perfect scores only prove the detectors and
the generator agree about what a fault looks like. Real recordings, made by people
who have never heard of baglens, are the only way to turn that table into a claim.

review.px4.io hosts ~450k public flights *with real failures* — the single richest
source of adversarial timing data that is free and needs no login.

The listing is a DataTables server-side endpoint; ``browse_data_retrieval`` needs the
full parameter set or it answers 400. Rows are HTML, so the log UUID is parsed out of
the anchor in column 1.

Usage:
    python scripts/fetch_px4.py --dest ~/data/public/px4 --count 120 --budget-gb 6
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

LIST_URL = "https://review.px4.io/browse_data_retrieval"
DOWNLOAD_URL = "https://review.px4.io/download?log={uuid}"
HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "baglens-corpus/1.0 (+https://github.com/baglens)",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://review.px4.io/browse",
}

_UUID_RE = re.compile(r"log=([0-9a-fA-F-]{36})")
_TAG_RE = re.compile(r"<[^>]+>")
_DUR_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


@dataclass
class LogEntry:
    uuid: str
    date: str
    airframe_type: str
    airframe: str
    hardware: str
    sw_version: str
    duration_s: int
    flight_modes: str


def _strip(html: str) -> str:
    return " ".join(_TAG_RE.sub(" ", str(html)).split())


def _parse_duration(text: str) -> int:
    m = _DUR_RE.fullmatch(text.strip())
    if not m:
        return 0
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mi * 60 + s


def list_logs(start: int, length: int) -> tuple[int, list[LogEntry]]:
    """One page of the listing. Returns (total_available, entries)."""
    params: list[tuple[str, str]] = [
        ("draw", "1"),
        ("start", str(start)),
        ("length", str(length)),
        ("search[value]", ""),
        ("search[regex]", "false"),
        ("order[0][column]", "0"),
        ("order[0][dir]", "desc"),
    ]
    for i in range(10):
        params += [
            (f"columns[{i}][data]", str(i)),
            (f"columns[{i}][searchable]", "true"),
            (f"columns[{i}][orderable]", "true"),
            (f"columns[{i}][search][value]", ""),
            (f"columns[{i}][search][regex]", "false"),
        ]
    req = urllib.request.Request(
        LIST_URL + "?" + urllib.parse.urlencode(params), headers=HEADERS
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))

    entries: list[LogEntry] = []
    for row in payload.get("data", []):
        match = _UUID_RE.search(str(row[1]))
        if not match:
            continue
        entries.append(
            LogEntry(
                uuid=match.group(1),
                date=_strip(row[1]),
                airframe_type=_strip(row[3]),
                airframe=_strip(row[4]),
                hardware=_strip(row[5]),
                sw_version=_strip(row[6]),
                duration_s=_parse_duration(_strip(row[7])),
                flight_modes=_strip(row[9]) if len(row) > 9 else "",
            )
        )
    return int(payload.get("recordsTotal", 0)), entries


def download(entry: LogEntry, dest: Path, seen_hashes: dict[str, str]) -> int:
    """Download one log. Returns bytes written (0 if already present, duplicate or failed).

    review.px4.io serves the same flight under more than one UUID — in the first corpus
    pulled here, 3 of 9 files were byte-identical to another. Left in, one popular flight
    is counted several times and every corpus statistic quietly becomes a statement about
    that flight, so duplicates are dropped by content hash.
    """
    target = dest / f"{entry.uuid}.ulg"
    if target.exists():
        return 0
    part = target.with_suffix(".ulg.part")
    req = urllib.request.Request(
        DOWNLOAD_URL.format(uuid=entry.uuid), headers={"User-Agent": HEADERS["User-Agent"]}
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp, part.open("wb") as fh:
            written = 0
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
                written += len(chunk)
    except Exception as exc:  # noqa: BLE001 - a dead log should not kill the corpus run
        part.unlink(missing_ok=True)
        print(f"  !! {entry.uuid}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0

    # A ULog file starts with the magic "ULog" + version byte. Anything else is an
    # error page with a 200, which is worse than a failure because it audits fine.
    with part.open("rb") as fh:
        if fh.read(4) != b"ULog":
            part.unlink(missing_ok=True)
            print(f"  !! {entry.uuid}: not a ULog file, discarded", file=sys.stderr)
            return 0

    digest = hashlib.sha1(part.read_bytes()).hexdigest()
    if digest in seen_hashes:
        part.unlink(missing_ok=True)
        print(f"  == {entry.uuid}: same flight as {seen_hashes[digest][:8]}, skipped")
        return 0
    seen_hashes[digest] = entry.uuid

    part.rename(target)
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dest", default="~/data/public/px4")
    ap.add_argument("--count", type=int, default=120, help="how many logs to keep")
    ap.add_argument("--budget-gb", type=float, default=6.0, help="stop after this much data")
    ap.add_argument("--min-duration", type=int, default=90,
                    help="seconds; skip flights too short to establish a cadence baseline")
    ap.add_argument("--max-duration", type=int, default=1800,
                    help="seconds; skip very long flights to keep the corpus broad, not deep")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--max-pages", type=int, default=60)
    args = ap.parse_args()

    dest = Path(args.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    budget = int(args.budget_gb * (1 << 30))

    manifest_path = dest / "manifest.json"
    manifest: list[dict[str, object]] = []
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    known = {str(m["uuid"]) for m in manifest}

    total_bytes = 0
    kept = 0
    print(f"destination: {dest}")

    # Hash what is already on disk so a resumed run does not re-import a duplicate.
    seen_hashes: dict[str, str] = {}
    for existing in sorted(dest.glob("*.ulg")):
        seen_hashes[hashlib.sha1(existing.read_bytes()).hexdigest()] = existing.stem

    for page in range(args.max_pages):
        if kept >= args.count or total_bytes >= budget:
            break
        try:
            available, entries = list_logs(page * args.page_size, args.page_size)
        except Exception as exc:  # noqa: BLE001
            print(f"listing page {page} failed: {exc}", file=sys.stderr)
            break
        if page == 0:
            print(f"{available} public flights available\n")
        if not entries:
            break

        for entry in entries:
            if kept >= args.count or total_bytes >= budget:
                break
            if not args.min_duration <= entry.duration_s <= args.max_duration:
                continue
            if entry.uuid in known:
                continue
            written = download(entry, dest, seen_hashes)
            if not written:
                continue
            total_bytes += written
            kept += 1
            known.add(entry.uuid)
            manifest.append(asdict(entry) | {"size_bytes": written})
            print(f"[{kept:>4}/{args.count}] {entry.uuid}  "
                  f"{entry.duration_s:>5}s  {written / 1e6:>7.1f} MB  "
                  f"{entry.hardware} {entry.sw_version}")
            manifest_path.write_text(json.dumps(manifest, indent=2))

    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\nkept {kept} logs, {total_bytes / 1e9:.2f} GB")
    print(f"manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
