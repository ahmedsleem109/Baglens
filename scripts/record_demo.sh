#!/usr/bin/env bash
# Record the README demo to docs/assets/demo.gif.
#
#   scripts/record_demo.sh ~/data/public/px4/588ff157-*.ulg
#
# Needs asciinema (`uv tool install asciinema`) and agg
# (https://github.com/asciinema/agg/releases — a single static binary).
#
# --idle-time-limit 2 caps the audit's silent stretch. The audit really does take ~20s
# on a 312s / 845k-message flight, and the demo prints that number on screen rather than
# letting the compressed playback imply it was instant.
set -euo pipefail

BAG="${1:?usage: record_demo.sh <recording>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/docs/assets"
mkdir -p "$OUT"

rm -f "$OUT/demo.cast"
asciinema rec "$OUT/demo.cast" \
  --cols 96 --rows 26 \
  --idle-time-limit 2 \
  --command "cd '$ROOT' && uv run --extra ulog python scripts/demo.py '$BAG'"

agg --font-size 15 --theme asciinema --line-height 1.4 \
    "$OUT/demo.cast" "$OUT/demo.gif"

echo "wrote $OUT/demo.gif ($(du -h "$OUT/demo.gif" | cut -f1))"
