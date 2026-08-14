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

# The Foxglove sample assets that used to live here are gone — assets.foxglove.dev
# now answers "Not found" for both nuScenes-v1.0-mini-scene-0061.mcap and demo.mcap.
# The MCAP project's own test corpus is small but stays put:
fetch "https://github.com/foxglove/mcap/raw/main/testdata/mcap/demo.mcap" \
      "mcap-demo.mcap" || echo "  (skipped: URL unavailable)"

# PX4 public flight logs — the source that actually matters.
# ~450k real flights with real failures, and each ULog carries the logger's own
# dropout records, which makes it the only public corpus with *labels we did not write*.
echo
echo "fetching PX4 flight logs (this is the corpus behind evals/integrity/REAL_DATA.md)"
python3 "$(dirname "$0")/fetch_px4.py" --dest "$DEST/px4" --count "${PX4_COUNT:-120}" \
        --budget-gb "${PX4_BUDGET_GB:-6}" || echo "  (skipped: PX4 fetch failed)"

cat <<'NOTE'

One more source worth pulling by hand, because it needs a browse step:

  * Hugging Face — search for `rosbag2` or `mcap` datasets.

Scoring against the PX4 corpus (needs the ulog extra: uv sync --extra ulog):

  uv run python -m evals.integrity.real_data --dir ~/data/public/px4

And the synthetic matrix, which is still the only source of labelled faults across
all eight classes:

  uv run python -m tests.synth.generate --matrix --out ~/data/synthetic
NOTE
