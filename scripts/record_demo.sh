#!/usr/bin/env bash
# Record the README demos to docs/assets/*.gif.
#
#   scripts/record_demo.sh audit  ~/data/public/px4/588ff157-*.ulg
#   scripts/record_demo.sh refuse ~/data/public/ros2/nuway_waypoints.mcap \
#                                 ~/data/public/ros2/nuway_stops.mcap
#   scripts/record_demo.sh gate   ~/data/demo_episodes
#
# Needs asciinema (`uv tool install asciinema`) and agg
# (https://github.com/asciinema/agg/releases — a single static binary).
#
# --idle-time-limit 2 caps each demo's silent stretch. The audit really does take ~20s on
# a 312s / 845k-message flight, and the demo prints that number on screen rather than
# letting the compressed playback imply it was instant.
#
# 118 columns is not cosmetic: `health.topic_timeline` prints a 72-column density map
# behind a topic-name column sized to the widest topic in the recording, and a PX4 flight
# carries names like `/magnetometer_bias_estimate`. At 96 columns every row wrapped and
# the demo's whole point — four topics dying in the same instant — became unreadable.
# Check the last frame of a re-recorded GIF before committing it; wrapping is silent.
set -euo pipefail

MODE="${1:?usage: record_demo.sh <audit|refuse|gate> <paths...>}"
shift
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/docs/assets"
mkdir -p "$OUT"

case "$MODE" in
  audit)
    NAME="demo"
    CMD="uv run --extra ulog python scripts/demo.py '${1:?need a recording}'"
    ;;
  refuse)
    NAME="demo-refuse"
    CMD="uv run python scripts/demo_refuse.py '${1:?need a healthy recording}' '${2:?need an unassessable one}'"
    ;;
  gate)
    NAME="demo-gate"
    CMD="uv run python scripts/demo_gate.py '${1:?need a directory of episodes}'"
    ;;
  *)
    echo "unknown mode '$MODE' — expected audit, refuse or gate" >&2
    exit 2
    ;;
esac

rm -f "$OUT/$NAME.cast"
asciinema rec "$OUT/$NAME.cast" \
  --cols 118 --rows 30 \
  --idle-time-limit 2 \
  --command "cd '$ROOT' && PYTHONPATH=. $CMD"

agg --font-size 15 --theme asciinema --line-height 1.4 \
    "$OUT/$NAME.cast" "$OUT/$NAME.gif"

echo "wrote $OUT/$NAME.gif ($(du -h "$OUT/$NAME.gif" | cut -f1))"
