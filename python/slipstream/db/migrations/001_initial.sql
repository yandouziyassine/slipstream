-- Initial schema: append-only results database (see docs/superpowers/specs/
-- 2026-09-27-collector-and-leaderboard-design.md, section 4).
--
-- Every table is append-only. UPDATE and DELETE are blocked by triggers that
-- RAISE(ABORT, 'append-only'). The one exception is `runs`: a single
-- started -> completed/failed/abandoned transition is allowed, which may set
-- `status`, `ended_at` and `error` only; every other column must be unchanged.

BEGIN TRANSACTION;

CREATE TABLE schema_version (
    version INTEGER NOT NULL PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('started', 'completed', 'failed', 'abandoned')),
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty REAL NOT NULL,
    duration_s INTEGER NOT NULL,
    fees_json TEXT NOT NULL,
    venue_rules_json TEXT NOT NULL,
    git_commit TEXT,
    error TEXT,
    supersedes_id INTEGER REFERENCES runs (id)
);

CREATE TABLE recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs (id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE recording_deletions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs (id),
    deleted_at TEXT NOT NULL
);

CREATE TABLE results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs (id),
    algo TEXT NOT NULL,
    state TEXT NOT NULL,
    filled_qty REAL NOT NULL,
    filled_pct REAL NOT NULL,
    avg_price REAL NOT NULL,
    arrival_mid REAL NOT NULL,
    slippage_bps REAL NOT NULL,
    fee_bps REAL NOT NULL,
    all_in_bps REAL NOT NULL,
    immediate_cost_bps REAL NOT NULL,
    routing_gain_bps REAL,
    venue_costs_json TEXT NOT NULL,
    fills_count INTEGER NOT NULL,
    halt_reason TEXT
);

CREATE TABLE fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs (id),
    algo TEXT NOT NULL,
    venue TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    ts_ns INTEGER NOT NULL
);

CREATE TABLE engine_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs (id),
    latency_p50_ns INTEGER NOT NULL,
    latency_p99_ns INTEGER NOT NULL,
    events INTEGER NOT NULL
);

CREATE INDEX results_run_id_idx ON results (run_id);
CREATE INDEX fills_run_id_idx ON fills (run_id);
CREATE INDEX recordings_run_id_idx ON recordings (run_id);
CREATE INDEX recordings_recorded_at_idx ON recordings (recorded_at);

CREATE TRIGGER schema_version_no_update
BEFORE UPDATE ON schema_version
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER schema_version_no_delete
BEFORE DELETE ON schema_version
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

-- `runs`: block every UPDATE except a single started -> terminal transition that leaves every
-- column but status/ended_at/error unchanged. The WHEN clause fires (aborting) whenever the
-- attempted update is NOT that one allowed shape.
CREATE TRIGGER runs_no_update_except_finish
BEFORE UPDATE ON runs
WHEN NOT (
    OLD.status = 'started'
    AND NEW.status IN ('completed', 'failed', 'abandoned')
    AND OLD.id IS NEW.id
    AND OLD.started_at IS NEW.started_at
    AND OLD.side IS NEW.side
    AND OLD.qty IS NEW.qty
    AND OLD.duration_s IS NEW.duration_s
    AND OLD.fees_json IS NEW.fees_json
    AND OLD.venue_rules_json IS NEW.venue_rules_json
    AND OLD.git_commit IS NEW.git_commit
    AND OLD.supersedes_id IS NEW.supersedes_id
)
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER runs_no_delete
BEFORE DELETE ON runs
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER recordings_no_update
BEFORE UPDATE ON recordings
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER recordings_no_delete
BEFORE DELETE ON recordings
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER recording_deletions_no_update
BEFORE UPDATE ON recording_deletions
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER recording_deletions_no_delete
BEFORE DELETE ON recording_deletions
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER results_no_update
BEFORE UPDATE ON results
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER results_no_delete
BEFORE DELETE ON results
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER fills_no_update
BEFORE UPDATE ON fills
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER fills_no_delete
BEFORE DELETE ON fills
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER engine_stats_no_update
BEFORE UPDATE ON engine_stats
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER engine_stats_no_delete
BEFORE DELETE ON engine_stats
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

INSERT INTO schema_version (version, applied_at) VALUES (1, strftime('%Y-%m-%dT%H:%M:%S', 'now'));

COMMIT;
