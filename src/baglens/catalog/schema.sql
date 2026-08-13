-- The fleet catalog. Index once, query forever: this is what makes a 500-mission
-- question answerable at all, because no catalog.* or compare.* query ever reopens a bag.

CREATE TABLE IF NOT EXISTS missions (
  mission_id      TEXT PRIMARY KEY,   -- content hash, stable across moves and renames
  path            TEXT,
  format          TEXT,               -- mcap | db3 | bag1 | ulog
  robot_id        TEXT,
  start_time      TIMESTAMP,
  end_time        TIMESTAMP,
  duration_s      DOUBLE,
  message_count   BIGINT,
  size_bytes      BIGINT,
  health_score    DOUBLE,
  verdict         TEXT,
  index_version   INTEGER,            -- re-index only what is stale when kernels improve
  indexed_at      TIMESTAMP,
  metadata        JSON
);

CREATE TABLE IF NOT EXISTS topics (
  mission_id  TEXT,
  topic       TEXT,
  msg_type    TEXT,
  count       BIGINT,
  expected_hz DOUBLE,
  actual_hz   DOUBLE,
  hz_source   TEXT,
  jitter_cv   DOUBLE,
  gap_count   INTEGER,
  max_gap_s   DOUBLE,
  total_silent_s DOUBLE,
  estimated_dropped BIGINT,
  score       DOUBLE,
  qos         JSON,
  PRIMARY KEY (mission_id, topic)
);

CREATE TABLE IF NOT EXISTS events (      -- detected, not recorded
  mission_id TEXT,
  finding_id TEXT,
  t          DOUBLE,
  t_end      DOUBLE,
  kind       TEXT,                       -- gap | dropped | jitter | clock | correlation | ...
  topic      TEXT,
  severity   INTEGER,
  summary    TEXT,
  detail     JSON
);

CREATE TABLE IF NOT EXISTS signals (     -- a pointer to Parquet, never the data
  mission_id   TEXT,
  signal_key   TEXT,                     -- "/odom.twist.twist.linear.x"
  parquet_path TEXT,
  sample_hz    DOUBLE,
  n            BIGINT,
  min DOUBLE, max DOUBLE, mean DOUBLE, stddev DOUBLE,
  p50 DOUBLE, p95 DOUBLE, p99 DOUBLE,
  PRIMARY KEY (mission_id, signal_key)
);

CREATE TABLE IF NOT EXISTS tags (
  mission_id TEXT, tag TEXT, source TEXT, created_at TIMESTAMP,
  PRIMARY KEY (mission_id, tag)
);

CREATE TABLE IF NOT EXISTS log_patterns (
  mission_id TEXT, template TEXT, level TEXT, count BIGINT, example TEXT,
  PRIMARY KEY (mission_id, template, level)
);

CREATE TABLE IF NOT EXISTS sources (
  root TEXT PRIMARY KEY, pattern TEXT, added_at TIMESTAMP
);
