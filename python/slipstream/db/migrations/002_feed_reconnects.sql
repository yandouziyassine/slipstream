-- Feed reconnects (see docs/superpowers/specs/2026-09-28-feed-reconnect-design.md).
--
-- One row per reconnect of a run's live feed ('feed') or of its recorder ('recorder'), with
-- the attempt number, the failure that caused it, and how long the venue was down after it.
-- Append-only like every table in 001: UPDATE and DELETE are blocked by triggers.

BEGIN TRANSACTION;

CREATE TABLE feed_reconnects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs (id),
    source TEXT NOT NULL CHECK (source IN ('feed', 'recorder')),
    venue TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    reason TEXT NOT NULL,
    downtime_s REAL NOT NULL CHECK (downtime_s >= 0),
    recovered INTEGER NOT NULL CHECK (recovered IN (0, 1))
);

CREATE INDEX feed_reconnects_run_id_idx ON feed_reconnects (run_id);

CREATE TRIGGER feed_reconnects_no_update
BEFORE UPDATE ON feed_reconnects
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER feed_reconnects_no_delete
BEFORE DELETE ON feed_reconnects
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

INSERT INTO schema_version (version, applied_at) VALUES (2, strftime('%Y-%m-%dT%H:%M:%S', 'now'));

COMMIT;
