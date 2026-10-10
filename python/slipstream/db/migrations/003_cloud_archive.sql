-- Cloud archive ledger (see docs/superpowers/specs/2026-09-28-cloud-archive-design.md, section 7.1).
--
-- `archive_days` has one row per UTC day whose recordings and fills were committed to the
-- off-site dataset and verified there. `recording_uploads` has one row per recording in such a
-- commit. Local retention deletes a recording only when its run has an upload row.
-- Append-only like every table in 001: UPDATE and DELETE are blocked by triggers.

BEGIN TRANSACTION;

CREATE TABLE archive_days (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL UNIQUE,
    commit_oid TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    fills_path TEXT NOT NULL,
    fills_sha256 TEXT NOT NULL,
    fills_rows INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE recording_uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL UNIQUE REFERENCES runs (id),
    archive_day_id INTEGER NOT NULL REFERENCES archive_days (id),
    path_in_repo TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL
);

CREATE TRIGGER archive_days_no_update
BEFORE UPDATE ON archive_days
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER archive_days_no_delete
BEFORE DELETE ON archive_days
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER recording_uploads_no_update
BEFORE UPDATE ON recording_uploads
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER recording_uploads_no_delete
BEFORE DELETE ON recording_uploads
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

INSERT INTO schema_version (version, applied_at) VALUES (3, strftime('%Y-%m-%dT%H:%M:%S', 'now'));

COMMIT;
