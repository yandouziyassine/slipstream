# Cloud Archive (Supabase Results, Hugging Face Recordings) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status: DRAFT — do not execute until the user approves the spec** (`docs/superpowers/specs/2026-09-28-cloud-archive-design.md`) and has answered its open questions. The answers may change T3, T8 and T12.

**Goal:** Build an off-site, free copy of the evidence:
- finished runs go to Supabase Postgres every hour;
- recordings and raw fills go to a private Hugging Face dataset once a day, verified before any local deletion.

Collection is never slowed down or blocked.

**Spec:** `docs/superpowers/specs/2026-09-28-cloud-archive-design.md`. Read it first. Section numbers below (§) refer to it.

**Tech stack:**
- Python 3.12.
- New dependencies: `psycopg[binary]` (3.3.x) and `huggingface_hub` (2.0.x), hash-pinned.
- Standard library for everything else: `hashlib`, `hmac`, `secrets`, `base64`, `gzip`, `csv`, `json`, `sqlite3`.
- PostgreSQL 16 server binaries, for tests only (`initdb` and `pg_ctl` on a Unix socket).

**Rules for every task (CLAUDE.md):**
- **TDD:** red, then green, then refactor.
- **Tests:** run only the task's tests while working. Run the full `bash scripts/ci.sh` once per PR (T11).
- **Reviews:** each implementer does a security and anomaly self-review of its own diff. The whole PR then gets `/security-review` and `/caveman:caveman-review` before any push.
- **Commits:**
  - Conventional commits, with the noreply author/committer set through env vars and the `Co-Authored-By` trailer.
  - Git runs from Windows; commands run in WSL through scratchpad scripts.
- **Secrets:** no task reads, writes or prints `.env`. Tests use a temporary env file with obviously fake values (e.g. `hf_` + 34×`x`).
- **Network:** no test touches the network. Supabase is the local fixture cluster, and Hugging Face is a fake `HubClient` or a stub `HfApi`.
- **File ownership:** strict. A task edits only its listed files. Interfaces between tasks are fixed below. If one must change, stop and report; don't edit another task's files.
- **Reads:** the new modules may read SQLite through `db.connection` with read-only queries, as `site/render.py` does. Every *write* goes through a `ResultsDB` method.

---

## Waves and file ownership

| Wave | Task | Owns (creates or edits) | Depends on |
|---|---|---|---|
| 0 | **T0** deps and Postgres tooling | `python/requirements-dev.in`, `python/requirements-dev.txt`, `python/pyproject.toml`, `scripts/setup_wsl.sh`, `scripts/ci.sh`, `.github/workflows/ci.yml` | — |
| 1 | **T1** shared redaction | `python/slipstream/redaction.py`, `python/slipstream/site/render.py` (import swap only), `python/tests/test_redaction.py` | — |
| 1 | **T2** migration 002 and archive DB methods | `python/slipstream/db/migrations/002_cloud_archive.sql`, `python/slipstream/db/results_db.py`, `python/tests/test_results_db_archive.py` | — |
| 1 | **T3** Supabase schema and the Postgres fixture | `cloud/supabase/001_schema.sql`, `python/tests/conftest.py` (append the `pg_cluster` fixture only), `python/tests/pg_cluster.py`, `python/tests/test_supabase_schema.py` | T0 |
| 1 | **T4** cloud settings and the enable switch | `python/slipstream/cloud/__init__.py`, `python/slipstream/cloud/settings.py`, `python/tests/test_cloud_settings.py` | — |
| 1 | **T9** Hub adapter | `python/slipstream/cloud/hub.py`, `python/tests/test_hub.py` | T0 |
| 2 | **T5** SCRAM verifier helper | `python/slipstream/cloud/scram.py`, `python/tests/test_scram.py` | T3, T4 |
| 2 | **T7** Supabase sync | `python/slipstream/cloud/supabase_sync.py`, `python/tests/test_supabase_sync.py` | T1, T2, T3, T4 |
| 2 | **T8** Hugging Face daily archive | `python/slipstream/cloud/hf_archive.py`, `python/tests/test_hf_archive.py` | T1, T2, T4, T9 |
| 2 | **T10** retention when the cloud is on | `python/slipstream/storage.py`, `python/slipstream/collect.py`, `python/tests/test_storage_cloud.py` | T2, T4 |
| 3 | **T11** CLI and hourly wiring | `python/slipstream/cloud/__main__.py`, `scripts/collect_hourly.sh`, `python/tests/test_cloud_main.py` | all of wave 2 |
| 3 | **T12** dataset card, user steps, manual rollout | `cloud/hf/README.md`; controller only: `input.md`, and the note/README lines through the docs owner | T11 |

Tasks within a wave share no files and can run concurrently in separate worktrees. Wave 1 tasks that don't depend on T0 (T1, T2, T4) can start immediately, alongside T0.

---

## Fixed interfaces (all tasks code against these)

```python
# slipstream/redaction.py (T1)
def redact_paths(text: str) -> str: ...                              # moved from site/render.py, same regexes
def redact(text: str, secrets: Iterable[str], limit: int = 300) -> str: ...
    # every non-empty secret -> "<redacted>", then redact_paths, then truncate to `limit`

# slipstream/cloud/__init__.py (T4)
def is_enabled(data_dir: Path) -> bool: ...                          # (data_dir / "cloud.enabled").exists()

# slipstream/cloud/settings.py (T4)
class CloudConfigError(RuntimeError): ...                            # message names the key, never the value
@dataclass(frozen=True)
class SupabaseSettings:
    pooler_host: str
    project_ref: str
    password: str = field(repr=False)
    @property
    def user(self) -> str: ...                                       # f"slipstream_collector.{project_ref}"
@dataclass(frozen=True)
class HubSettings:
    token: str = field(repr=False)
    repo_id: str = "yandouziyassine/slipstream-recordings"
def env_file_path() -> Path: ...                                     # $SLIPSTREAM_ENV_FILE or <repo root>/.env
def load_supabase(env_file: Path) -> SupabaseSettings: ...           # raises CloudConfigError
def load_hub(env_file: Path) -> HubSettings: ...                     # raises CloudConfigError
def ca_path(data_dir: Path) -> Path: ...                             # data_dir / "cloud" / "supabase-ca.crt"

# slipstream/db/results_db.py (T2), new
@dataclass(frozen=True)
class RecordingUpload:
    run_id: int
    path_in_repo: str
    sha256: str
    bytes: int
@dataclass(frozen=True)
class DayRecording:
    run_id: int
    path: Path
    sha256: str
    started_at: datetime                                             # the run's start: names the repo path
    recorded_at: datetime                                            # goes into the manifest
    qty: float
@dataclass(frozen=True)
class FillRow:
    fill_id: int
    run_id: int
    algo: str
    venue: str
    qty: float
    price: float
    fee: float
    ts_ns: int
class ResultsDB:
    def archive_ready_days(self, today: date) -> list[date]: ...     # §6.2 readiness rule, oldest first
    def day_recordings(self, day: date) -> list[DayRecording]: ...   # runs started on `day`, not deleted, by run_id
    def day_fills(self, day: date) -> list[FillRow]: ...             # fills of runs started on `day`, by fill id
    def record_archive_day(
        self, day: date, commit_oid: str, manifest_path: str, manifest_sha256: str,
        fills_path: str, fills_sha256: str, fills_rows: int,
        uploads: Sequence[RecordingUpload], uploaded_at: datetime,
    ) -> None: ...                                                   # one transaction
    def uploaded_recordings_older_than(self, now: datetime, days: int) -> list[tuple[int, Path]]: ...
    def unarchived_recordings_older_than(self, now: datetime, days: int) -> int: ...  # count only
    def active_recordings(self, uploaded_first: bool = False) -> list[tuple[int, Path]]: ...
    def is_uploaded(self, run_id: int) -> bool: ...

# slipstream/cloud/hub.py (T9): exactly the spec's §6.3 block, plus
class HfHubClient:                                                   # implements HubClient
    def __init__(self, settings: HubSettings, api: HfApiLike | None = None) -> None: ...

# slipstream/cloud/scram.py (T5)
def scram_sha256_verifier(password: str, salt: bytes | None = None, iterations: int = 4096) -> str: ...
def alter_role_statement(verifier: str) -> str: ...                  # "ALTER ROLE slipstream_collector PASSWORD '<v>';"

# slipstream/cloud/supabase_sync.py (T7)
@dataclass(frozen=True)
class SyncReport:
    runs_sent: int
    rows_sent: dict[str, int]
    db_bytes: int
def connection_kwargs(settings: SupabaseSettings, ca: Path) -> dict[str, str | int]: ...
def connect(settings: SupabaseSettings, ca: Path) -> psycopg.Connection[Any]: ...   # refuses when `ca` is missing
def sync(remote: psycopg.Connection[Any], db: ResultsDB, data_dir: Path,
         log: logging.Logger, secrets: Sequence[str], max_runs: int = 500,
         size_limits: tuple[int, int] = (400 * 2**20, 450 * 2**20)) -> SyncReport: ...

# slipstream/cloud/hf_archive.py (T8)
@dataclass(frozen=True)
class ArchiveReport:
    days_archived: list[date]
    days_failed: list[date]
def archive(db: ResultsDB, data_dir: Path, hub: HubClient, now: datetime,
            log: logging.Logger, secrets: Sequence[str],
            budget_s: float = 1200.0, clock: Callable[[], float] = time.monotonic) -> ArchiveReport: ...
```

---

## Wave 0

### T0. Dependencies and Postgres tooling (`chore/cloud-deps`)

**Files:** listed in the table.

- [ ] Add `psycopg[binary]>=3.3` and `huggingface_hub>=2.0` to `requirements-dev.in`.
- [ ] Regenerate `requirements-dev.txt` with the exact command in its header. Never hand-edit it.
- [ ] Run `pip-audit -r requirements-dev.txt` and confirm it is clean.
- [ ] `pyproject.toml`: add a mypy override only if `mypy slipstream` reports a package with no types. Both packages are expected to ship `py.typed`; check before adding anything. No new per-file lint ignores.
- [ ] `setup_wsl.sh`:
  - Add `postgresql` to `PACKAGES`.
  - After the install, run `sudo systemctl disable --now postgresql 2>/dev/null || true`. The tests run their own throwaway cluster, and no system service is wanted.
- [ ] `ci.yml`:
  - Add `postgresql` to the apt install line.
  - Add `SLIPSTREAM_REQUIRE_PG=1` to the env of the "Build, lint, test, audit" step.
- [ ] `ci.sh`: no change unless the Python test step needs `PATH` to include `$(pg_config --bindir)`. Prefer letting the T3 fixture find the binaries itself.
- [ ] Validate:
  - `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`;
  - `bash -n` on both scripts;
  - in WSL, `pip install --require-hashes -r python/requirements-dev.txt`, then `python -c "import psycopg, huggingface_hub"`.
- [ ] Commit: `chore(deps): add psycopg and huggingface_hub, install postgres for tests`.

---

## Wave 1

### T1. Shared redaction (`python/slipstream/redaction.py`)

- [ ] **Red:** `test_redaction.py`:
  - `redact_paths` keeps the exact behaviour of today's `site/render._redact_paths`. Copy its cases from `test_render.py`: POSIX and Windows paths become `<path>`, and URLs are kept.
  - `redact("x hf_abc y", ["hf_abc"])` gives `"x <redacted> y"`.
  - Empty secrets are ignored (no `<redacted>` everywhere).
  - A secret that also looks like a path is redacted as a secret.
  - Output is truncated to `limit`.
- [ ] **Green:**
  - Move the two regexes and the function into `redaction.py`.
  - `render.py` imports `redact_paths` and deletes its private copy. No behaviour change: `test_render.py` stays green, unchanged.
- [ ] Run `pytest tests/test_redaction.py tests/test_render.py -q`, then `ruff` and `mypy`.
- [ ] Commit: `refactor(site): move path redaction to slipstream.redaction`, then `feat: add secret redaction helper`.

### T2. Migration 002 and archive DB methods

- [ ] **Red:** `test_results_db_archive.py`, using a `ResultsDB` in `tmp_path`:
  - The migration applies to a v1 DB and to an empty file; `schema_version` holds 1 and 2; running `migrate()` twice is a no-op.
  - `UPDATE` and `DELETE` on `archive_days` and `recording_uploads` raise `sqlite3.IntegrityError`. A second upload row for the same `run_id` is refused.
  - `archive_ready_days(today)`:
    - excludes today, days with no runs, days already archived, and a day with a `started` run;
    - includes that day once the run is marked `abandoned`;
    - returns days oldest first.
  - `day_recordings`/`day_fills` return only that UTC day's runs, skip deleted recordings, and order by id.
  - `record_archive_day` writes both tables atomically: a bad upload row (unknown `run_id`) leaves neither.
  - `uploaded_recordings_older_than` returns only uploaded, not-deleted recordings past the cutoff. `unarchived_recordings_older_than` counts the others.
  - `active_recordings(uploaded_first=True)` lists all uploaded rows (oldest first), then the non-uploaded ones (oldest first). The default ordering is unchanged.
  - `is_uploaded`.
- [ ] **Green:**
  - Write the SQL from §7.1, with triggers in the style of `001_initial.sql` and `INSERT INTO schema_version (2, …)`.
  - Add the methods and dataclasses to `results_db.py` with parameterised SQL only. `day` is compared as `substr(started_at, 1, 10) = ?` with `day.isoformat()`.
- [ ] Run the test file plus `tests/test_results_db.py` and `tests/test_storage.py` (the existing ones stay green), then `ruff` and `mypy`.
- [ ] Commit: `feat(db): add migration 002 for the cloud archive ledger`.

### T3. Supabase schema and the Postgres test fixture

**`cloud/supabase/001_schema.sql`:**
- One transaction.
- `create schema slipstream`.
- The eight tables from §5.1, with CHECKs and FKs. `recording_uploads.archive_day_id` references `archive_days(id)`.
- The append-only trigger function `slipstream.append_only()` (`raise exception 'append-only'`), plus per-table row triggers and a statement-level `TRUNCATE` trigger.
- The role and grants from §5.2, with **no password clause**.
- It begins with a header comment: what it is, and "paste once into the Supabase SQL editor; it contains no secrets".
- **Idempotent failure:** re-running it fails on `create schema`, and nothing is half-applied. Do not add `if not exists` noise.

**`python/tests/pg_cluster.py` and `conftest.py`:**
- `PgCluster` finds `initdb`/`pg_ctl` via `pg_config --bindir`, falling back to `/usr/lib/postgresql/*/bin`.
- It runs `initdb -A trust` into `tmp_path_factory`, then `pg_ctl start -o "-c listen_addresses='' -k <sockdir>"`, and stops the cluster on teardown.
- If the binaries are missing, the fixture calls `pytest.skip`, or `pytest.fail` when `SLIPSTREAM_REQUIRE_PG=1`.
- The session-scoped fixture `pg_cluster` creates a fresh database per test (`CREATE DATABASE t_<n>`) with stub roles `anon` and `authenticated`, applies the schema file unchanged, and yields a helper exposing:
  - `connect(role: str | None = None)`, a superuser connection that runs `SET ROLE <role>` when a role is given;
  - `connect_as(user: str, password: str)`, a real login over the socket;
  - `set_hba(lines: Sequence[str])`, which rewrites `pg_hba.conf` and reloads (used by T5).
- Roles are cluster-wide, so the schema's `create role` runs once per cluster. The fixture applies the file's role section once and the schema section per database, or drops the role between tests. Pick one, and document it in `pg_cluster.py`.
- `conftest.py`: append only the fixture registration (`from pg_cluster import pg_cluster  # noqa: F401`, or define the fixture there, whichever import mode works). Touch nothing else in it.

**Steps:**
- [ ] **Red:** `test_supabase_schema.py`:
  - The schema applies.
  - As `slipstream_collector` (via `connect(role=...)`): `INSERT` and `SELECT` work on all eight tables; `UPDATE`, `DELETE` and `TRUNCATE` fail with `InsufficientPrivilege`.
  - As the superuser: `UPDATE`/`DELETE`/`TRUNCATE` fail with the `append-only` error.
  - As `anon` and `authenticated`: `SELECT` fails, and `USAGE` on the schema fails.
  - RLS is enabled on every table (`pg_class.relrowsecurity`).
  - `slipstream_collector` has no `BYPASSRLS` and no password (`rolpassword IS NULL` in `pg_authid`).
  - `statement_timeout` is set for the role.
- [ ] **Green:** write the SQL and the fixture.
- [ ] Run the test file.
- [ ] Commit: `feat(cloud): add the Supabase schema, role and grants`, then `test: add a throwaway Postgres cluster fixture`.

### T4. Cloud settings and the enable switch

- [ ] **Red:** `test_cloud_settings.py`, with a temp env file:
  - **Parser:** `KEY=VALUE`, blank lines and `#` comments, optional matching single or double quotes, keys other than the four ignored.
  - An `export KEY=...` line is rejected with an error that names the key.
  - A duplicated key is an error.
  - **Validation:** each regex and rule from §9.2, each tested accept and reject.
  - The error message contains the key name and **never** the value (assert the value is not in `str(exc)`).
  - `repr(settings)` never contains the password or token.
  - `.user` gives `slipstream_collector.<ref>`.
  - `env_file_path()` honours `SLIPSTREAM_ENV_FILE`.
  - `is_enabled` follows the flag file.
  - A missing env file gives a `CloudConfigError("… .env not found")`.
- [ ] **Green:** `settings.py` in plain Python with no `os.environ` mutation. `__init__.py` holds only `is_enabled`.
- [ ] Commit: `feat(cloud): parse and validate cloud credentials from .env`.

### T9. Hugging Face adapter (`hub.py`)

- [ ] **Red:** `test_hub.py` with a stub `HfApiLike`, a `Protocol` defined in `hub.py` with `repo_info`, `get_paths_info` and `create_commit`:
  - `head()` returns `repo_info(repo_id, repo_type="dataset", token=…).sha`.
  - `paths_info` passes `revision` and `repo_type="dataset"`, and maps `RepoFile.lfs.sha256`/`blob_id`/`size`. `lfs=None` becomes `sha256=None`. `RepoFolder` entries are ignored.
  - `commit` builds one `CommitOperationAdd(path_in_repo, path)` per file, passes `parent_commit` and `repo_type="dataset"`, and returns `CommitInfo.oid`.
  - It refuses more than 97 files, and any `path_in_repo` that is absolute, contains `..` or a backslash, or does not start with `recordings/`, `fills/` or `manifests/`.
  - Any exception from the stub (`HfHubHTTPError` with the token in its message, `OSError`) becomes a `HubError` with no token in the message.
  - The constructor sets `HF_HUB_DISABLE_TELEMETRY=1` via `os.environ.setdefault` before importing `huggingface_hub`. That is the only environment mutation, and it holds no secret.
- [ ] **Green:** implement it. `HfApi(token=settings.token)` is built lazily, so tests never import-time-touch the network.
- [ ] Commit: `feat(cloud): add a thin Hugging Face Hub adapter`.

---

## Wave 2

### T5. SCRAM verifier helper

- [ ] **Red:** `test_scram.py`:
  - The format matches `^SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+$`.
  - A fixed salt gives a fixed output (regression).
  - A non-ASCII password is refused (SASLprep is not implemented, so fail safe).
  - **Real check:** on `pg_cluster`, create a login role, `ALTER ROLE … PASSWORD '<verifier>'`, then check that `pg_authid.rolpassword` equals the verifier exactly. After switching `pg_hba.conf` to `scram-sha-256` for that role over the socket and reloading, a psycopg login with the plaintext succeeds and a wrong password fails. `PgCluster` exposes `set_hba(lines)` for this; ask T3's owner if it is missing rather than editing `pg_cluster.py`.
- [ ] **Green:**
  - RFC 5802/7677 key derivation:
    - `SaltedPassword = pbkdf2_hmac("sha256", pw, salt, 4096)`;
    - `ClientKey = HMAC(SaltedPassword, "Client Key")`;
    - `StoredKey = SHA256(ClientKey)`;
    - `ServerKey = HMAC(SaltedPassword, "Server Key")`.
  - Base64 encoding, with a 16-byte salt from `secrets.token_bytes`.
- [ ] Commit: `feat(cloud): compute SCRAM-SHA-256 verifiers locally`.

### T7. Supabase sync

- [ ] **Red:** `test_supabase_sync.py`, from a `ResultsDB` in `tmp_path` into `pg_cluster`. The remote connection is a superuser connection that ran `SET ROLE slipstream_collector` first, so privilege checks and RLS apply as the collector.
  - **Round trip:**
    - 3 completed runs and 1 failed run, with results, fills, stats and recordings, give the same `runs`/`results`/`engine_stats`/`recordings` rows remotely.
    - `fill_summaries` equal a `GROUP BY` computed in the test.
    - `rel_path` is relative.
    - `error` is path-redacted.
    - A `started` run is not sent.
  - **Idempotence:** a second `sync` sends 0 rows.
  - **Late close:**
    - run 1 is `started`, and runs 2–3 complete and sync;
    - `mark_abandoned` closes run 1;
    - the next `sync` sends run 1, through the `k > n` path.
  - **Remote ahead:** a remote run id unknown locally (inserted by the superuser) makes `sync` log an error and send nothing.
  - **Watermark tables:** `recording_deletions`, `archive_days` and `recording_uploads` rows added after the first sync arrive on the next one. An FK failure rolls back only that table and is logged.
  - **Guard:** an injected `size_limits=(small, smaller)` skips the pass with the `supabase_near_limit` event.
  - **Batch cap:** `max_runs=2` with 5 pending runs sends 2, then the rest on later passes.
  - **No local path:** scan every text and jsonb value remotely for `str(tmp_path)` and `str(Path.home())`; nothing is found.
  - A recording path outside `$DATA/recordings` makes that run skipped and logged.
  - **Secrets:** an exception whose message contains a fake secret, raised through a patched execute, is logged without it.
  - **`connection_kwargs`:** `sslmode == "verify-full"`, `sslrootcert == str(ca)`, `port == 5432`, `dbname == "postgres"`, `user == settings.user`, `connect_timeout == 10`.
  - `connect()` with a missing CA raises before any socket is opened.
- [ ] **Green:** implement §5.3 exactly. Use one `with remote.transaction():` per run and per watermark table, `psycopg.types.json.Jsonb` for the JSON columns, and `datetime` values with `tzinfo=UTC`.
- [ ] Commit: `feat(cloud): sync finished runs to Supabase, insert-only`.

### T8. Hugging Face daily archive

- [ ] **Red:** `test_hf_archive.py`, with a `FakeHub` implementing `HubClient`. It stores files in a dict keyed by `path_in_repo`, holding the sha256, size and a git blob id, tracks the head oid, and can be told to fail commit, fail verify, or return tampered metadata.
  - **Readiness:** for a DB with runs on D-2, D-1 and today, where D-1 has a `started` run, one commit is made for D-2.
  - **Contents:**
    - the commit holds `recordings/YYYY/MM/DD/HH-<qty>.jsonl.gz` for each recording, `fills/YYYY/MM/DD.csv.gz` and `manifests/YYYY/MM/DD.jsonl`;
    - the manifest lines match the §6.1 schema;
    - the CSV header and rows match `day_fills`.
  - **Determinism:** building the day twice gives byte-identical fills and manifest files, with a gzip `mtime` of 0 and no file name.
  - **Recording:**
    - after a verified commit, the `archive_days` and `recording_uploads` rows exist, with the commit oid;
    - `$DATA/cloud/staging/<D>` is removed;
    - a second `archive` does nothing.
  - **Reconcile:**
    - the remote already holds identical files (simulate a crash after the commit): no new commit is made, and the local rows are written;
    - the remote holds a different sha for one path: the day fails, nothing is written, and the error event is logged.
  - **Verification failure:** the fake returns a wrong size or sha: nothing is recorded, and the day is in `days_failed`.
  - **Safety:**
    - a `recordings.path` of `tmp_path/"outside"/"id_ed25519"` is never read or committed;
    - a symlink inside `recordings/` pointing outside is refused;
    - a local file whose sha differs from the DB is left out, with the day still archived and the file absent from the manifest.
  - **Limits:**
    - more than 97 files for a day fails the day, which never happens with 48 recordings; the test guards the invariant;
    - the budget `clock` stops a second day from starting.
  - **Logs:** the fake hub raises a `HubError` whose text contains the fake token passed in `secrets`, and no log record contains it.
- [ ] **Green:**
  - Implement §6.2.
  - The git blob id of a small file is `sha1(b"blob %d\0" % len(data) + data)`.
  - Build paths from `DayRecording.started_at` (date and hour, the same `now` that named the local file) and `qty`, through a private `_qty_token` in `hf_archive.py` whose output is checked against the §6.1 regex. Do not import `collect`'s private helper.
- [ ] Commit: `feat(cloud): archive each finished day to Hugging Face, verified`.

### T10. Retention when the cloud is on

- [ ] **Red:** `test_storage_cloud.py`:
  - **Flag on:**
    - a recording older than 7 days that is uploaded is deleted, with its file unlinked and a `recording_deletions` row;
    - one that is **not** uploaded is kept, and `recordings_awaiting_upload` is logged with its count;
    - noon thinning and 90-day expiry do not run.
  - **Budget with the flag on:**
    - the uploaded recordings are evicted before older non-uploaded ones;
    - evicting a non-uploaded one logs `recordings_evicted_unarchived` with its count;
    - this hour's recordings are never evicted.
  - **Flag off:** the existing `test_storage.py` and `test_collect.py` pass unchanged.
- [ ] **Green:**
  - `enforce_recordings_budget(..., uploaded_first: bool = False)` uses `db.active_recordings(uploaded_first=...)` and returns the evicted paths plus an unarchived count; `collect._housekeeping` logs it. Adjust the return type, and update the only caller in `collect.py`.
  - A new `retire_uploaded_recordings(db, now, days) -> list[Path]` goes in `storage.py`.
  - `_housekeeping` branches on `cloud.is_enabled(cfg.data_dir)` at hour 0.
- [ ] Commit: `feat(collect): delete local recordings only after a verified upload`.

---

## Wave 3

### T11. CLI and hourly wiring

**`python -m slipstream.cloud <command>`:**

| Command | Needs `cloud.enabled` | What it does |
|---|---|---|
| `sync` | yes | Take `$DATA/cloud.lock`, load Supabase settings, `connect`, `sync`, log the report. |
| `archive` | yes | Take the lock, load hub settings, `archive`. |
| `check` | no | Try each service. Supabase: TLS `verify-full`, then `select current_user, pg_database_size(current_database())`. HF: `head()`. Prints `supabase: OK (db 12.3 MB)` / `supabase: FAIL <redacted summary>` / `huggingface: OK` / `FAIL …`. Exits 1 if any fails. Never prints a secret. |
| `scram-verifier` | no | Load the Supabase password from `.env` and print `alter_role_statement(scram_sha256_verifier(pw))`, and nothing else. |

- Logs go to `$DATA/logs/cloud-<date>.log` (JSON, `JsonFormatter`).
- Every command catches its own errors at the top, like `publish.py`, and returns nonzero instead of raising.
- A disabled flag logs `cloud_disabled` and returns 0.

**`collect_hourly.sh`** — append after the site steps:

```bash
# Off-site copy (spec 2026-09-28-cloud-archive). Best-effort, low priority, hard-capped so the next
# hour's collection always starts on an idle machine; a failure here never fails the hour.
LOWPRI=(nice -n 10)
command -v ionice >/dev/null 2>&1 && LOWPRI+=(ionice -c3)
"${LOWPRI[@]}" timeout 5m python -m slipstream.cloud sync || echo "cloud sync failed" >&2
"${LOWPRI[@]}" timeout 25m python -m slipstream.cloud archive || echo "cloud archive failed" >&2
```

**Steps:**
- [ ] **Red:** `test_cloud_main.py`:
  - disabled leads to a no-op, exit 0, and no settings are loaded;
  - a held lock gives "skipped", exit 0;
  - `scram-verifier` output matches the `ALTER ROLE` pattern and does not contain the plaintext;
  - `check` with the connectors monkeypatched to fail gives exit 1 and redacted output;
  - an invalid `.env` value gives `cloud_config_invalid: <KEY>` with no value.
- [ ] **Green:** implement.
- [ ] `bash -n scripts/collect_hourly.sh`.
- [ ] Full `bash scripts/ci.sh`. It must print `CI OK`, with the Postgres tests actually running (not skipped) under `SLIPSTREAM_REQUIRE_PG=1`.
- [ ] Whole-PR `/security-review` and `/caveman:caveman-review`, then fix the findings test-first.
- [ ] Commit: `feat(cloud): add the cloud CLI and wire it into the hourly run`.
- [ ] **Stop.** The controller asks the user before any push or PR.

### T12. Dataset card, user steps and manual rollout

**`cloud/hf/README.md`:** a dataset card, pasted by the user when creating the dataset. It covers:
- what the files are (§6.1 layout; the raw public-WebSocket messages, with `recv_ns`);
- that these are **paper-trading** experiments;
- the source venues and a note that exchange terms govern the data (no license is granted over exchange data);
- the manifest schema;
- how to verify a file (sha256);
- a link to the GitHub repo.
- It contains no personal data.

The controller writes this to `input.md`, and tells the user in chat:

````markdown
## Cloud archive setup (spec 2026-09-28-cloud-archive-design.md), after you approve the spec
What: a free Supabase project (results) and a free private Hugging Face dataset (recordings, raw fills).
Why: off-site, verified copies of the evidence, so local recordings can be deleted safely.
You never paste a secret into chat. The assistant never sees .env.

### Supabase
1. Sign up at https://supabase.com/dashboard (GitHub sign-in is fine). Stay on the **Free** plan; never add a card.
2. New project: name `slipstream`, region = <your answer to open question 6>. Set a strong password for the
   `postgres` user and keep it in your password manager only (the collector never uses it, and it must NOT
   go into .env). If you see "Automatically expose new tables", leave it **unchecked**.
3. Project Settings → Database → SSL Configuration: turn **on** "Enforce SSL on incoming connections", then
   "Download certificate". In WSL:
   `mkdir -p ~/slipstream-data/cloud && cp "/mnt/c/Users/yando/Downloads/prod-ca-2021.crt" ~/slipstream-data/cloud/supabase-ca.crt`
4. SQL Editor → New query → paste all of `cloud/supabase/001_schema.sql` from the repo → Run. Expect "Success. No rows returned".
5. In WSL run `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. Then add these to
   `C:\Code\slipstream\.env` yourself:
       SLIPSTREAM_SUPABASE_COLLECTOR_PASSWORD=<the value just printed>
       SLIPSTREAM_SUPABASE_PROJECT_REF=<Project Settings → General → Project ID>
       SLIPSTREAM_SUPABASE_POOLER_HOST=<Connect → "Session pooler" → host, like aws-0-<region>.pooler.supabase.com>
6. In WSL, from the repo: `PYTHONPATH=python ~/.venvs/slipstream/bin/python -m slipstream.cloud scram-verifier`.
   It prints one line starting `ALTER ROLE slipstream_collector PASSWORD 'SCRAM-SHA-256$...`. Paste that
   line into a new SQL Editor query and Run. (The verifier is a salted hash; your password itself never leaves the PC.)

### Hugging Face
7. Sign up at https://huggingface.co/join. Stay on the free plan (no PRO).
8. https://huggingface.co/new-dataset → owner `yandouziyassine`, name `slipstream-recordings`,
   visibility **Private** (unless you answered "public" to open question 1). Then open the dataset's
   README (dataset card) → Edit → paste `cloud/hf/README.md` from the repo → Commit.
9. https://huggingface.co/settings/tokens → Create new token → **Fine-grained** → name `slipstream-archive-pc`
   → "Repositories permissions": search and select `yandouziyassine/slipstream-recordings`, tick
   **"Write access to contents/settings of selected repos"** only → leave every other box unticked → Create.
   Add it to .env yourself: `SLIPSTREAM_HF_TOKEN=hf_...`

### Turn it on
10. In WSL, from the repo: `PYTHONPATH=python ~/.venvs/slipstream/bin/python -m slipstream.cloud check`.
    Expect `supabase: OK (db … MB)` and `huggingface: OK`. Tell the assistant the two lines (they contain no secrets).
11. `touch ~/slipstream-data/cloud.enabled`. The next hourly run syncs, and the first run after 00:00 UTC archives.
What the assistant does next: checks the first sync's row counts and the first archive's manifest with you,
and records the Supabase baseline size in note.md.
````

**Manual rollout (controller with the user, after the merge):**
1. The user completes steps 1–10. `check` is OK. If TLS fails with a certificate error, stop: it may be the pooler chain issue [R7]. Never relax `sslmode`.
2. `python -m slipstream.cloud sync` by hand. Compare `select count(*)` per table in the SQL editor with the local counts.
3. `touch cloud.enabled`, then wait for the first run after 00:00 UTC. Open the dataset's `manifests/…jsonl` and compare its SHA-256s with `recordings.sha256` locally.
4. Record in `note.md` (through the docs owner): the Supabase baseline size, the first-week DB growth, the real fills/run, and the real recording MB.

---

## Self-review checklist (for the plan executor)

- [ ] No task logs a path, token, password or DSN. `grep -rn "exc_info" slipstream/cloud` is empty.
- [ ] No code path writes `.env`, calls `huggingface_hub.login`, or sets `HF_TOKEN`/`PGPASSWORD`.
- [ ] No `sslmode` other than `verify-full` appears anywhere.
- [ ] No remote `UPDATE`/`DELETE`/`TRUNCATE`, and no `ON CONFLICT DO UPDATE`.
- [ ] No local deletion happens without `is_uploaded`, except in the disk-cap branch, which logs.
- [ ] Every repo path comes from validated parts, and every uploaded file resolves inside `$DATA/recordings` or `$DATA/cloud/staging`.
- [ ] The `cloud.enabled` flag absent means today's behaviour exactly, and the existing tests are unchanged.
