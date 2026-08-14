# baglens tool-surface eval

- cases: **56**
- pass rate: **100.0%**
- mean correctness: 100.0%
- mean tool-call efficiency: 100.0%
- mean tokens per case: 706 (total 39,549)
- uncited claims: **0.96%**

| case | correct | calls | tokens | uncited | failed assertion |
|---|---|---|---|---|---|
| `anomalies_mad` | 100% | 1 | 246 | 0% |  |
| `changepoints` | 100% | 1 | 199 | 0% |  |
| `clean_bag_is_clean` | 100% | 1 | 1,367 | 0% |  |
| `clean_bag_no_phantom_drops` | 100% | 1 | 1,367 | 0% |  |
| `clock_step_located` | 100% | 1 | 2,074 | 0% |  |
| `clock_step_not_duplicated` | 100% | 1 | 498 | 0% |  |
| `cohort_split_by_verdict` | 100% | 1 | 157 | 0% |  |
| `compare_two_missions` | 100% | 1 | 717 | 0% |  |
| `compare_windows_effect_size` | 100% | 1 | 235 | 0% |  |
| `contact_sheet_built` | 100% | 1 | 248 | 0% |  |
| `correlate_with_lag` | 100% | 1 | 130 | 0% |  |
| `diffuse_drops_diagnosis` | 100% | 1 | 1,769 | 0% |  |
| `diffuse_drops_estimated` | 100% | 1 | 1,769 | 0% |  |
| `event_timeline_merged` | 100% | 1 | 226 | 0% |  |
| `explain_finding_gives_rule` | 100% | 2 | 2,490 | 10% |  |
| `export_foxglove_handoff` | 100% | 1 | 131 | 0% |  |
| `export_plot_html` | 100% | 1 | 110 | 0% |  |
| `export_report_cited` | 100% | 1 | 633 | 0% |  |
| `export_trim_evidence` | 100% | 1 | 113 | 0% |  |
| `field_stats_cheap` | 100% | 1 | 153 | 0% |  |
| `find_similar_has_reasons` | 100% | 1 | 308 | 0% |  |
| `fleet_summary_answers` | 100% | 1 | 685 | 0% |  |
| `fleet_worst_missions_listed` | 100% | 1 | 685 | 0% |  |
| `gap_found` | 100% | 1 | 1,986 | 0% |  |
| `gap_isolated_not_stall` | 100% | 1 | 260 | 0% |  |
| `gap_window_accurate` | 100% | 1 | 260 | 0% |  |
| `get_message_at` | 100% | 1 | 258 | 0% |  |
| `index_status_reports` | 100% | 1 | 37 | 0% |  |
| `jitter_expansion` | 100% | 1 | 1,649 | 0% |  |
| `keyframes_extracted` | 100% | 1 | 194 | 0% |  |
| `list_missions_filtered` | 100% | 1 | 462 | 0% |  |
| `list_topics` | 100% | 1 | 335 | 0% |  |
| `log_patterns_compress` | 100% | 1 | 314 | 0% |  |
| `log_query_filtered` | 100% | 1 | 279 | 0% |  |
| `missing_file_no_crash` | 100% | 1 | 101 | 0% |  |
| `mission_info_from_index` | 100% | 1 | 608 | 0% |  |
| `qos_mismatch_reported` | 100% | 1 | 1,626 | 0% |  |
| `rank_by_gaps` | 100% | 1 | 320 | 0% |  |
| `rate_degradation_caveat` | 100% | 1 | 2,068 | 0% |  |
| `rate_degradation_trend` | 100% | 1 | 2,068 | 0% |  |
| `recorder_lag_curve` | 100% | 1 | 613 | 0% |  |
| `recorder_lag_finding` | 100% | 1 | 1,734 | 0% |  |
| `regression_scan_runs` | 100% | 1 | 299 | 0% |  |
| `sample_messages_capped` | 100% | 1 | 249 | 0% |  |
| `search_missions_free_text` | 100% | 1 | 312 | 0% |  |
| `stall_finding_explains` | 100% | 1 | 1,713 | 0% |  |
| `stall_is_system_wide` | 100% | 1 | 526 | 0% |  |
| `tag_persists` | 100% | 1 | 611 | 0% |  |
| `tf_report_links` | 100% | 1 | 110 | 0% |  |
| `timeline_shows_shape` | 100% | 1 | 712 | 0% |  |
| `timeseries_extract_binned` | 100% | 1 | 278 | 0% |  |
| `timeseries_gaps_not_interpolated` | 100% | 1 | 560 | 0% |  |
| `topic_schema_before_query` | 100% | 1 | 506 | 0% |  |
| `trajectory_summary` | 100% | 1 | 145 | 0% |  |
| `truncated_file_audit_still_works` | 100% | 1 | 1,852 | 0% |  |
| `truncated_file_degrades` | 100% | 1 | 194 | 0% |  |

Run in 93.8s, deterministic mode (reference tool sequences, no model in the loop). Model-in-the-loop scoring is the same harness with `--model`, which is opt-in because it costs money and cannot run in CI.
