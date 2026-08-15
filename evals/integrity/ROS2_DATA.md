# Real ROS 2 recordings

**11 recordings, 5 platforms, 64 minutes, 6.3 GB.** Formats exercised: `db3`, `mcap`. Generated 2026-08-16.

10 of them, across 4 platforms, were written by `ros2 bag record` on the robot itself. That distinction is load-bearing: a recording converted into MCAP from something else was re-timestamped on the way, so its arrival stream belongs to the converter and no claim about *recorder* behaviour can rest on it. Converted recordings are still worth auditing — they exercise the readers on real topic sets and real message mixes — but they are marked, and they are not evidence about recorders.

**There are no labels here, so there is no precision or recall on this page.** Nothing in these recordings says where a fault was. What this shows is that the readers open real ROS 2 files written by other people's tooling, that the audit completes in bounded memory, and what the detectors claim — each finding printed with its evidence so it can be judged rather than believed. The only measured precision and recall in this repository are in `REAL_DATA.md`, against PX4's own dropout records.

## By platform

| Platform | Recordings | Native | Minutes | Topics (max) | Verdicts |
|---|---|---|---|---|---|
| autonomous shuttle bus | 2 | 2 | 56 | 110 | 1× trustworthy, 1× unassessable |
| handheld LiDAR-inertial-visual rig | 1 | 0 | 2 | 4 | 1× trustworthy |
| quadruped / IMU rig | 1 | 1 | 1 | 12 | 1× trustworthy |
| road vehicle (Tesla Model 3) | 1 | 1 | 2 | 40 | 1× usable_with_caveats |
| short-run rig | 6 | 6 | 3 | 11 | 6× trustworthy |

## Every recording

| Recording | Platform | Format | Native | Size | Duration | Topics | Messages | Verdict | Score | Audit | Peak RSS |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `dongkkka_00` | short-run rig | `mcap` | yes | 167 MB | 37s | 11 | 30,222 | trustworthy | 99.9 | 1.9s | 38 MB |
| `dongkkka_01` | short-run rig | `mcap` | yes | 161 MB | 36s | 11 | 29,149 | trustworthy | 100.0 | 2.4s | 38 MB |
| `dongkkka_02` | short-run rig | `mcap` | yes | 139 MB | 31s | 11 | 25,075 | trustworthy | 99.8 | 1.6s | 38 MB |
| `dongkkka_03` | short-run rig | `mcap` | yes | 135 MB | 30s | 11 | 24,571 | trustworthy | 100.0 | 1.6s | 38 MB |
| `dongkkka_04` | short-run rig | `mcap` | yes | 151 MB | 33s | 11 | 27,173 | trustworthy | 100.0 | 1.8s | 38 MB |
| `dongkkka_05` | short-run rig | `mcap` | yes | 140 MB | 31s | 11 | 25,370 | trustworthy | 99.9 | 1.8s | 38 MB |
| `fastlivo_hku2` | handheld LiDAR-inertial-visual rig | `mcap` | converted | 886 MB | 105s | 4 | 24,456 | trustworthy | 100.0 | 9.3s | 38 MB |
| `nuway_stops` | autonomous shuttle bus | `mcap` | yes | 1317 MB | 1492s | 110 | 90,856 | unassessable | 98.7 | 27.2s | 196 MB |
| `nuway_waypoints` | autonomous shuttle bus | `mcap` | yes | 72 MB | 1843s | 5 | 200,144 | trustworthy | 95.0 | 9.8s | 196 MB |
| `tesla3_av` | road vehicle (Tesla Model 3) | `mcap` | yes | 1098 MB | 148s | 40 | 315,493 | usable_with_caveats | 84.2 | 26.0s | 196 MB |
| `uniflex_imu` | quadruped / IMU rig | `db3` | yes | 1996 MB | 44s | 12 | 19,345 | trustworthy | 100.0 | 19.0s | 196 MB |

## Known limitation, stated before the results

Some of these recordings are mostly **event-driven** topics — topics with no cadence to be late against, which publish when something happens. `nuway_stops` (70 of 110). On those, `correlation` over-reports: several topics falling quiet at once looks like the recorder stopping, and a stationary vehicle does that constantly. The score and verdict below should not be read as a judgement of the recording.

The obvious fix — refusing to let event-driven topics count — was tried four ways and each cost 22+ points of recall against the PX4 labels, because when the recorder truly stops those topics stop too. Rather than tune against data with no labels, the limitation is published. See W15 in `PHASE3.md`.

## Every finding, so it can be judged

### `dongkkka_00` — short-run rig

Source: Dongkkka/rosbag_test. 0 of 11 topics have no measurable cadence.

No findings.


### `dongkkka_01` — short-run rig

Source: Dongkkka/rosbag_test. 0 of 11 topics have no measurable cadence.

No findings.


### `dongkkka_02` — short-run rig

Source: Dongkkka/rosbag_test. 0 of 11 topics have no measurable cadence.

No findings.


### `dongkkka_03` — short-run rig

Source: Dongkkka/rosbag_test. 0 of 11 topics have no measurable cadence.

No findings.


### `dongkkka_04` — short-run rig

Source: Dongkkka/rosbag_test. 0 of 11 topics have no measurable cadence.

No findings.


### `dongkkka_05` — short-run rig

Source: Dongkkka/rosbag_test. 0 of 11 topics have no measurable cadence.

No findings.


### `fastlivo_hku2` — handheld LiDAR-inertial-visual rig

Source: DapengFeng/MCAP. 0 of 4 topics have no measurable cadence.

No findings.


### `nuway_stops` — autonomous shuttle bus

Source: xrkong/nuway_rosbag. 70 of 110 topics have no measurable cadence.

| Detector | Topic | Window | Severity | Claim |
|---|---|---|---|---|
| `correlation` | `—` | 3.7–11.8s | 4 | system-wide stall: 48 topics silent together for 8.05s |
| `correlation` | `/global_costmap/published_footprint` | 4.1–11.3s | 3 | subsystem failure: /global_costmap/published_footprint and 22 related topics silent together for 7.24s |
| `correlation` | `/lidar/localisation_merged/cloud` | 4.3–11.3s | 3 | subsystem failure: /lidar/localisation_merged/cloud and 30 related topics silent together for 7.02s |
| `correlation` | `/lidar/localisation_merged/scan` | 4.3–11.4s | 3 | subsystem failure: /lidar/localisation_merged/scan and 34 related topics silent together for 7.10s |
| `correlation` | `/lidar/velodyne/front/raw` | 4.3–11.4s | 3 | subsystem failure: /lidar/velodyne/front/raw and 32 related topics silent together for 7.01s |
| `correlation` | `/lidar/velodyne/front/cloud` | 4.4–11.4s | 3 | subsystem failure: /lidar/velodyne/front/cloud and 33 related topics silent together for 7.00s |
| `correlation` | `/battery_voltage` | 4.4–11.3s | 3 | subsystem failure: /battery_voltage and 20 related topics silent together for 6.95s |
| `correlation` | `/battery_percent` | 4.4–11.3s | 3 | subsystem failure: /battery_percent and 21 related topics silent together for 6.95s |
| `correlation` | `/lidar/velodyne/rear/raw` | 4.4–11.4s | 3 | subsystem failure: /lidar/velodyne/rear/raw and 35 related topics silent together for 7.08s |
| `correlation` | `/lidar_safety/rear_left/cloud` | 4.4–11.3s | 3 | subsystem failure: /lidar_safety/rear_left/cloud and 29 related topics silent together for 6.92s |
| `correlation` | `/lidar_safety/rear_right/cloud` | 4.4–11.3s | 3 | subsystem failure: /lidar_safety/rear_right/cloud and 32 related topics silent together for 6.92s |
| `correlation` | `/lidar_localisation/rear/cloud` | 4.4–11.3s | 3 | subsystem failure: /lidar_localisation/rear/cloud and 29 related topics silent together for 6.92s |
| `correlation` | `/lidar_localisation/front/cloud` | 4.4–11.3s | 3 | subsystem failure: /lidar_localisation/front/cloud and 30 related topics silent together for 6.91s |
| `correlation` | `/local_costmap/costmap_raw` | 4.4–11.3s | 3 | subsystem failure: /local_costmap/costmap_raw and 34 related topics silent together for 6.91s |
| `correlation` | `/drive_cmds_stamped` | 4.4–11.3s | 3 | subsystem failure: /drive_cmds_stamped and 33 related topics silent together for 6.90s |
| … | | | | 71 more |


### `nuway_waypoints` — autonomous shuttle bus

Source: xrkong/nuway_rosbag. 0 of 5 topics have no measurable cadence.

| Detector | Topic | Window | Severity | Claim |
|---|---|---|---|---|
| `dropped` | `/odometry/global` | 0.2–1843.4s | 2 | /odometry/global is missing ~2145 messages (4.7% of expected) |
| `gap` | `/odometry/global` | 1.4–1.8s | 1 | /odometry/global silent for 0.35s (9x its 25.5 Hz period) |


### `tesla3_av` — road vehicle (Tesla Model 3)

Source: tfoldi/tesla3_av_rosbags. 3 of 40 topics have no measurable cadence.

| Detector | Topic | Window | Severity | Claim |
|---|---|---|---|---|
| `rate_degradation` | `/from_can_bus` | 5.0–135.0s | 3 | /from_can_bus slowed by 4% over 130s (1715.1 → 1650.3 Hz), peaking at 65% within the window |
| `rate_degradation` | `/diagnostics` | 8.2–148.0s | 1 | /diagnostics sped up by 13% over 140s (6.7 → 7.5 Hz) |
| `aperiodic` | `/rosout` | 0.0–148.0s | 0 | /rosout published 44 times in 3.3s — too few to establish a rate |
| `aperiodic` | `/ntrip_client/rtcm` | 1.2–148.0s | 0 | /ntrip_client/rtcm has no stable publication rate — 390 messages over 145.7s, arriving in bursts |
| `aperiodic` | `/ubx_rxm_rtcm` | 4.2–148.0s | 0 | /ubx_rxm_rtcm has no stable publication rate — 1077 messages over 142.8s, arriving in bursts |


### `uniflex_imu` — quadruped / IMU rig

Source: UniflexAI/rosbag2_imu_example. 3 of 12 topics have no measurable cadence.

| Detector | Topic | Window | Severity | Claim |
|---|---|---|---|---|
| `aperiodic` | `/camera/camera/extrinsics/depth_to_infra1` | 0.0–43.9s | 0 | /camera/camera/extrinsics/depth_to_infra1 published 1 times in 0.0s — too few to establish a rate |
| `aperiodic` | `/camera/camera/extrinsics/depth_to_infra2` | 0.0–43.9s | 0 | /camera/camera/extrinsics/depth_to_infra2 published 1 times in 0.0s — too few to establish a rate |
| `aperiodic` | `/tf` | 0.2–43.9s | 0 | /tf published 44 times in 43.0s — too few to establish a rate |

> no metadata.yaml — topic list reconstructed from SQLite schema

