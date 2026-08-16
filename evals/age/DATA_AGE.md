# F1 — end-to-end data age: precision and recall

Regenerate with `uv run python -m evals.age.data_age`. Every number below comes from that command; none is transcribed by hand.

Generated in 465s.

## The claim the whole feature rests on

`header.stamp` is read as an 8-byte peek at CDR offset 4 rather than by deserializing. That is verified separately, against a full decode, by `scripts/verify_stamp_peek.py` — see `docs/how-it-works.md`. If that check ever fails on a corpus, none of the numbers here mean anything on it.

## Synthetic pipelines

| | |
|---|---|
| precision | 1.000 |
| recall | 1.000 |
| ramps injected | 15 |
| clean pipelines | 5 |

Ramp magnitudes are multiples of the healthy stage delay: x2, x4, x8. Seeds: 11, 22, 33, 44, 55.

### Rules that are not the trend

* unmeasurable stage named, not invented: **True**
* restamping node caught: **True**
* propagation graph inferred correctly: **True**

The graph is inferred from stamp equality alone — nothing declares it:

```
{
  "/camera/image_raw": null,
  "/cmd_vel_stamped": "/detections",
  "/detections": "/camera/image_raw"
}
```

Per-stage delay recovered (ms, P50):

```
{
  "/camera/image_raw": null,
  "/cmd_vel_stamped": 36.78,
  "/detections": 81.5
}
```

## Real background, injected fault

| | |
|---|---|
| precision | 0.900 |
| recall | 0.750 |
| ramps injected | 12 |

Scored differentially: each base is also copied clean, and any trend finding present in that control is subtracted before the faulted copy is scored.

### Data age measured on the unmodified recordings

This is the number F1 exists to produce, and it is reported here on real robots rather than on fixtures.

#### nuway_stops.mcap (600s)

| topic | P50 ms | P95 ms | P99 ms | messages |
|---|---:|---:|---:|---:|
| `/CameraFront` | 7.7 | 20.9 | 37.4 | 258 |
| `/CameraRear` | 7.5 | 26.2 | 39.4 | 256 |
| `/candata` | 1.6 | 32.0 | 88.5 | 2,327 |
| `/diagnostics` | 1.4 | 9.4 | 26.2 | 3,670 |
| `/drive_cmds_stamped` | 1.7 | 86.6 | 169.3 | 1,211 |
| `/driving_mode_stamped` | 1.9 | 45.7 | 187.1 | 991 |
| `/gear_stamped` | 1.9 | 32.4 | 151.1 | 991 |
| `/global_costmap/costmap` | 1152.4 | 1152.4 | 1152.4 | 2 |
| `/global_costmap/published_footprint` | 113.2 | 285.8 | 285.8 | 18 |
| `/imu/data` | 1.9 | 175.7 | 317.7 | 677 |
| `/imu/mag` | 1.6 | 54.1 | 135.2 | 1,330 |
| `/imu/nav_sat_fix` | 1.8 | 18.5 | 155.9 | 133 |
| `/imu/odometry` | 2.1 | 169.5 | 321.6 | 665 |
| `/imu/pos_ecef` | 1.7 | 56.0 | 146.6 | 1,331 |
| `/imu/temp` | 1.6 | 82.0 | 249.7 | 670 |
| `/imu/velocity` | 1.6 | 16.2 | 58.6 | 3,289 |
| `/joy` | 1.6 | 84.0 | 171.4 | 1,147 |
| `/joy_cmd_vel_stamped` | 2.0 | 84.3 | 173.2 | 797 |
| `/lidar/localisation_merged/cloud` | 2.4 | 32.7 | 121.3 | 266 |
| `/lidar/localisation_merged/scan` | 5.1 | 13.4 | 33.1 | 232 |
| `/lidar/velodyne/front/cloud` | 6.5 | 24.2 | 78.5 | 137 |
| `/lidar/velodyne/front/raw` | 1.1 | 21.1 | 73.0 | 137 |
| `/lidar/velodyne/rear/cloud` | 5.1 | 27.9 | 100.5 | 139 |
| `/lidar/velodyne/rear/raw` | 2.0 | 17.4 | 95.9 | 138 |
| `/lidar_localisation/front/cloud` | 1.1 | 7.9 | 32.9 | 326 |
| `/lidar_localisation/rear/cloud` | 2.3 | 12.8 | 40.2 | 329 |
| `/lidar_safety/front_left/cloud` | 49.2 | 57.9 | 86.5 | 654 |
| `/lidar_safety/front_right/cloud` | 49.0 | 58.2 | 85.5 | 653 |
| `/lidar_safety/rear_left/cloud` | 49.2 | 58.4 | 74.1 | 653 |
| `/lidar_safety/rear_right/cloud` | 48.9 | 59.1 | 100.1 | 655 |
| `/lidar_safety/sick_lms_1xx/lferec` | 5.0 | 7.6 | 7.6 | 8 |
| `/lidar_safety/sick_lms_1xx/lidoutputstate` | 4.0 | 6.6 | 6.6 | 12 |
| `/lidar_safety/sick_lms_1xx/scan` | 48.6 | 57.1 | 84.6 | 2,614 |
| `/local_costmap/costmap` | 2.9 | 11.5 | 43.7 | 338 |
| `/local_costmap/costmap_raw` | 3.1 | 12.6 | 43.2 | 339 |
| `/local_costmap/published_footprint` | 52.9 | 131.1 | 193.2 | 525 |
| `/nn_cmd_vel_stamped` | 3.5 | 991.7 | 1166.0 | 161 |
| `/odom_pub` | 1.2 | 56.1 | 192.7 | 321 |
| `/odometry/global` | 7.7 | 153.3 | 308.5 | 635 |
| `/odometry/gps` | 36.9 | 721.1 | 1405.8 | 135 |
| `/rosout` | 9947.7 | 10006.1 | 10006.1 | 26 |
| `/sbg/ekf_euler` | 1.6 | 42.9 | 130.4 | 1,330 |
| `/sbg/ekf_nav` | 1.6 | 39.7 | 125.6 | 1,329 |
| `/sbg/gps_hdt` | 1.8 | 19.8 | 157.5 | 133 |
| `/sbg/gps_pos` | 1.8 | 18.7 | 156.1 | 133 |
| `/sbg/gps_vel` | 1.7 | 18.6 | 156.9 | 129 |
| `/sbg/imu_data` | 1.8 | 137.4 | 291.9 | 656 |
| `/sbg/mag` | 1.7 | 53.1 | 137.5 | 1,330 |
| `/sbg/status` | 1.2 | 16.5 | 16.5 | 26 |
| `/scan` | 9.8 | 651.5 | 1424.5 | 141 |
| `/speed_mode_stamped` | 2.0 | 46.1 | 151.2 | 989 |

Unmeasurable — named, never given an age from arrival time:

* `/battery_percent` — std_msgs/msg/Float32 carries no header.stamp
* `/battery_voltage` — std_msgs/msg/Float32 carries no header.stamp
* `/bond` — stamps are on a different time base from the recorder, so no age can be computed from them
* `/drive_cmds` — std_msgs/msg/Float32MultiArray carries no header.stamp
* `/driving_mode` — std_msgs/msg/Int32 carries no header.stamp
* `/driving_text_mode` — std_msgs/msg/String carries no header.stamp
* `/driving_text_mode_overlay_text` — rviz_2d_overlay_msgs/msg/OverlayText carries no header.stamp
* `/errorCodesDisp` — std_msgs/msg/String carries no header.stamp
* `/errorCodesDisp_overlay_text` — rviz_2d_overlay_msgs/msg/OverlayText carries no header.stamp
* `/error_codes` — std_msgs/msg/Int32MultiArray carries no header.stamp
* `/front_steering_fb` — std_msgs/msg/Float32 carries no header.stamp
* `/gear_float` — std_msgs/msg/Float32 carries no header.stamp
* `/joy_cmd_vel` — geometry_msgs/msg/Twist carries no header.stamp
* `/lidar_safety/sick_lms_1xx/marker` — visualization_msgs/msg/MarkerArray carries no header.stamp
* `/nn_cmd_vel` — geometry_msgs/msg/Twist carries no header.stamp
* `/rear_steering_fb` — std_msgs/msg/Float32 carries no header.stamp
* `/speed_fb` — std_msgs/msg/Float32 carries no header.stamp
* `/speed_mode` — std_msgs/msg/Float32 carries no header.stamp
* `/tf` — tf2_msgs/msg/TFMessage carries no header.stamp

#### nuway_waypoints.mcap (600s)

| topic | P50 ms | P95 ms | P99 ms | messages |
|---|---:|---:|---:|---:|
| `/imu/data` | 0.2 | 1.2 | 3.3 | 14,945 |
| `/imu/nav_sat_fix` | 0.2 | 1.3 | 2.5 | 2,989 |
| `/odometry/global` | 5.0 | 33.3 | 40.0 | 14,235 |
| `/sbg/ekf_nav` | 0.2 | 1.3 | 3.4 | 29,907 |
| `/sbg/gps_pos` | 0.2 | 1.4 | 3.2 | 2,989 |

#### tesla3_av.mcap (148s)

| topic | P50 ms | P95 ms | P99 ms | messages |
|---|---:|---:|---:|---:|
| `/diagnostics` | 1.0 | 6.0 | 11.5 | 1,066 |
| `/fix` | 7.5 | 13.5 | 19.6 | 1,452 |
| `/from_can_bus` | 1.3 | 5.2 | 10.7 | 301,319 |
| `/imu` | 0.8 | 7.7 | 10.8 | 1,456 |
| `/magnetometer` | 0.7 | 4.1 | 9.6 | 1,456 |
| `/ntrip_client/rtcm` | 1.4 | 5.7 | 11.3 | 390 |
| `/rosout` | 2.0 | 86.2 | 86.2 | 44 |
| `/scan` | 16.2 | 23.8 | 33.3 | 1,446 |
| `/ubx_nav_hp_pos_llh` | 7.2 | 12.8 | 18.7 | 1,452 |
| `/ubx_nav_status` | 8.1 | 13.7 | 18.7 | 1,452 |
| `/ubx_rxm_rtcm` | 7.5 | 12.6 | 18.3 | 1,077 |
| `/velodyne_packets` | 2.8 | 11.2 | 13.5 | 1,439 |
| `/velodyne_points` | 16.9 | 24.8 | 31.6 | 1,443 |

### Cases

| case | expected | detected | fired on |
|---|---|---|---|
| demo.mcap (skipped) | `—` | no | — |
| dongkkka_00.mcap (skipped) | `—` | no | — |
| dongkkka_01.mcap (skipped) | `—` | no | — |
| dongkkka_02.mcap (skipped) | `—` | no | — |
| dongkkka_03.mcap (skipped) | `—` | no | — |
| dongkkka_04.mcap (skipped) | `—` | no | — |
| dongkkka_05.mcap (skipped) | `—` | no | — |
| fastlivo_hku2.mcap (skipped) | `—` | no | — |
| nuway_stops.mcap x1 | `/diagnostics` | no | — |
| nuway_stops.mcap x2 | `/diagnostics` | yes | `/diagnostics` |
| nuway_stops.mcap x4 | `/diagnostics` | yes | `/diagnostics` |
| nuway_stops.mcap x8 | `/diagnostics` | yes | `/diagnostics` |
| nuway_waypoints.mcap x1 | `/imu/data` | no | — |
| nuway_waypoints.mcap x2 | `/imu/data` | yes | `/imu/data` |
| nuway_waypoints.mcap x4 | `/imu/data` | yes | `/imu/data` |
| nuway_waypoints.mcap x8 | `/imu/data` | yes | `/imu/data` |
| tesla3_av.mcap x1 | `/from_can_bus` | no | — |
| tesla3_av.mcap x2 | `/from_can_bus` | yes | `/from_can_bus` |
| tesla3_av.mcap x4 | `/from_can_bus` | yes | `/from_can_bus` |
| tesla3_av.mcap x8 | `/from_can_bus` | yes | `/from_can_bus` |

## Synthetic cases

| case | detail | detected | fired on |
|---|---|---|---|
| clean seed=11 | — | yes | — |
| clean seed=22 | — | yes | — |
| clean seed=33 | — | yes | — |
| clean seed=44 | — | yes | — |
| clean seed=55 | — | yes | — |
| ramp x2 seed=11 | 82 -> 164 ms | yes | `/detections`, `/cmd_vel_stamped` |
| ramp x4 seed=11 | 82 -> 328 ms | yes | `/detections`, `/cmd_vel_stamped` |
| ramp x8 seed=11 | 82 -> 656 ms | yes | `/detections`, `/cmd_vel_stamped` |
| ramp x2 seed=22 | 82 -> 164 ms | yes | `/detections`, `/cmd_vel_stamped` |
| ramp x4 seed=22 | 82 -> 328 ms | yes | `/detections`, `/cmd_vel_stamped` |
| ramp x8 seed=22 | 82 -> 656 ms | yes | `/detections`, `/cmd_vel_stamped` |
| ramp x2 seed=33 | 82 -> 164 ms | yes | `/detections`, `/cmd_vel_stamped` |
| ramp x4 seed=33 | 82 -> 328 ms | yes | `/detections`, `/cmd_vel_stamped` |
| ramp x8 seed=33 | 82 -> 656 ms | yes | `/detections`, `/cmd_vel_stamped` |
| ramp x2 seed=44 | 82 -> 164 ms | yes | `/detections`, `/cmd_vel_stamped` |
| ramp x4 seed=44 | 82 -> 328 ms | yes | `/detections`, `/cmd_vel_stamped` |
| ramp x8 seed=44 | 82 -> 656 ms | yes | `/detections`, `/cmd_vel_stamped` |
| ramp x2 seed=55 | 82 -> 164 ms | yes | `/detections`, `/cmd_vel_stamped` |
| ramp x4 seed=55 | 82 -> 328 ms | yes | `/detections`, `/cmd_vel_stamped` |
| ramp x8 seed=55 | 82 -> 656 ms | yes | `/detections`, `/cmd_vel_stamped` |
