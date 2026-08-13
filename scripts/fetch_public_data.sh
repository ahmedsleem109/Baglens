#!/usr/bin/env bash
# Download public robot recordings on demand and cache them.
#
# Disk discipline: keep total local data under ~20 GB. Everything here is free.
set -euo pipefail

DEST="${1:-$HOME/data/public}"
mkdir -p "$DEST"

fetch() {
  local url="$1" name="$2"
  if [ -f "$DEST/$name" ]; then
    echo "have  $name"
    return
  fi
  echo "fetch $name"
  curl -fSL --retry 3 -o "$DEST/$name.part" "$url" && mv "$DEST/$name.part" "$DEST/$name"
}

echo "destination: $DEST"
echo

# Foxglove sample recordings — small, well-formed, ideal fixtures.
fetch "https://assets.foxglove.dev/nuScenes-v1.0-mini-scene-0061.mcap" \
      "nuscenes-mini-scene-0061.mcap" || echo "  (skipped: URL unavailable)"

cat <<'NOTE'

Two more sources worth pulling by hand, because both need a browse step:

  * PX4 public flight logs — https://review.px4.io/browse
    Thousands of real flights with real failures. Download a few .ulg files here;
    baglens reads them with the `ulog` extra:  uv sync --extra ulog

  * Hugging Face — search for `rosbag2` or `mcap` datasets.

And the one that matters most for testing:

  uv run python -m tests.synth.generate --matrix --out ~/data/synthetic

That is the only source of *labelled* failures, and therefore the backbone of every
published number in this repository.
NOTE
