#!/usr/bin/env bash
# Real ROS 2 recordings, chosen for *platform diversity* rather than volume.
#
# Every claim this repository makes about ROS 2 rested on fixtures it generated itself
# until this corpus existed — which can only prove that the detectors and the generator
# agree. These are recordings written by other people's robots, with other people's
# tooling, and they are the input to `evals/integrity/ros2_data.py`.
#
# Chosen small-first and one-per-platform: three 6 GB logs from one robot prove less than
# six 150 MB logs from six. Where a dataset offers several sizes, the smallest that still
# carries a full topic set is taken.
#
#   scripts/fetch_ros2.sh [dest]
#
# ⚠️ DISK. The WSL ext4 image lives on the Windows D: drive and `df /` inside WSL reports
# the *virtual* size, which is meaningless. Filling the host drive once made ext4 remount
# read-only mid-write and the distro then would not boot. This script therefore checks the
# host filesystem before every download and refuses to start one it cannot finish.
set -uo pipefail

DEST="${1:-$HOME/data/public/ros2}"
#: refuse to download when the host filesystem would drop below this many GB
MIN_FREE_GB="${MIN_FREE_GB:-8}"
mkdir -p "$DEST"

host_free_gb() {
  # The Windows drive holding the WSL image, when we can see it; otherwise the rootfs,
  # which at least catches a genuinely full disk.
  local target=/mnt/d
  [ -d "$target" ] || target="$DEST"
  df -BG --output=avail "$target" 2>/dev/null | tail -1 | tr -dc '0-9'
}

get() {  # get <local-name> <hf-repo/path> <size-mb> <platform>
  local name="$1" path="$2" size_mb="$3" platform="$4"
  if [ -s "$DEST/$name" ]; then
    echo "have   $name  ($platform)"
    return 0
  fi
  local free
  free="$(host_free_gb)"
  local need_gb=$(( (size_mb / 1024) + MIN_FREE_GB ))
  if [ -n "$free" ] && [ "$free" -lt "$need_gb" ]; then
    echo "SKIP   $name — ${free}GB free, need ${need_gb}GB (${size_mb}MB + ${MIN_FREE_GB}GB margin)"
    return 1
  fi
  echo "fetch  $name  (~${size_mb}MB, $platform, ${free}GB free)"
  if curl -fsSL --retry 3 --max-time 3600 -o "$DEST/$name.part" \
      "https://huggingface.co/datasets/$path"; then
    mv "$DEST/$name.part" "$DEST/$name"
  else
    echo "  FAILED $name"
    rm -f "$DEST/$name.part"
    return 1
  fi
}

echo "destination: $DEST   host free: $(host_free_gb)GB"
echo

# -- autonomous shuttle bus (Nav2) — natively recorded rosbag2 MCAP -------------
# The most valuable recordings here: written by `ros2 bag record` on a real vehicle, so
# they are the only ones that can support a claim about recorder behaviour. Everything
# marked "converted" below was re-timestamped on its way into MCAP and cannot.
get nuway_waypoints.mcap \
    "xrkong/nuway_rosbag/resolve/main/rosbag2_2024_09_03-10_05_53_waypoints/rosbag2_2024_09_03-10_05_53_0.mcap" \
    72 "shuttle bus, native rosbag2"
get nuway_stops.mcap \
    "xrkong/nuway_rosbag/resolve/main/rosbag2_2024_09_03-10_05_53_stops/rosbag2_2024_09_03-10_05_53_0.mcap" \
    1318 "shuttle bus, native rosbag2"

# -- road vehicle — Tesla Model 3, native rosbag2 MCAP with lidar ---------------
get tesla3_av.mcap \
    "tfoldi/tesla3_av_rosbags/resolve/main/rosbag2_2024_02_10-14_24_59/rosbag2_2024_02_10-14_24_59_0.mcap" \
    1099 "Tesla Model 3, native rosbag2"

# -- rosbag2 SQLite — the only real `.db3` in the corpus ------------------------
# `.db3` is one of the four formats the README claims and the only one with no real-world
# coverage at all: every db3 test to date runs on a file this repository converted.
get uniflex_imu.db3 \
    "UniflexAI/rosbag2_imu_example/resolve/main/rosbag2_2026_03_04-16_52_43_0.db3" \
    1997 "quadruped/IMU rig, native rosbag2 sqlite"

# -- six short recordings from one rig, for repeat-run statistics ---------------
# Cheap recording *count*: the fleet layer needs several missions from one unit before it
# can claim a trend, and these are six for under a gigabyte.
for i in 0 1 2 3 4 5; do
  get "dongkkka_0${i}.mcap" \
      "Dongkkka/rosbag_test/resolve/main/${i}/${i}_0.mcap" \
      170 "short-run rig #${i}"
done

# -- handheld LiDAR-inertial-visual rig — CONVERTED, not natively recorded ------
get fastlivo_hku2.mcap \
    "DapengFeng/MCAP/resolve/main/FAST-LIVO/hku2/hku2_0.mcap" \
    886 "handheld LIVO rig, converted to MCAP"

echo
du -sh "$DEST"
ls -la "$DEST"
cat <<'NOTE'

Audit everything and regenerate the report:

  uv run python -m evals.integrity.ros2_data --dir ~/data/public/ros2 \
      --out evals/integrity/ROS2_DATA.md

More sources, if the corpus needs to grow (sizes are per recording):

  tfoldi/tesla3_av_rosbags       4 more Tesla runs, 1.3-2.3 GB   native rosbag2 mcap
  UniflexAI/rosbag2_go2_*        Unitree Go2 quadruped, 3-11 GB  native rosbag2 sqlite
  alvgaona/tii-ratm-rosbag2      6 UAV flights, 6.5-8 GB         native rosbag2 mcap
  Adapting/nuscenes-rosbags      10 nuScenes scenes, ~6 GB       ROS 1 .bag
  BreCaspian/ROBOMASTER-2025-*   2 competition robots, 12-14 GB  ROS 1 .bag
  DapengFeng/MCAP                41 SLAM sequences, 0.9-33 GB    converted

Check `df -h /mnt/d` before adding any of them.
NOTE
