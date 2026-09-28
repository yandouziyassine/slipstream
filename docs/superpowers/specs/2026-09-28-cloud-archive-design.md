# Cloud Archive: Supabase Results and Hugging Face Recordings — Design

Date: 2026-09-28
Status: **DRAFT — awaiting user approval.** The user chose the services (Supabase for results, Hugging Face Datasets for recordings). Every detail below is a proposal until the user approves it in chat.
Scope: an off-site, free, online copy of the evidence the hourly collector produces (see `2026-09-27-collector-and-leaderboard-design.md`). Builds on the storage budget from PR #26 (`python/slipstream/storage.py`).

## 1. Goal

The evidence only has value if it survives, and it currently lives on one disk:
- Every finished run's results reach a hosted Postgres database within the hour. The future API and MCP server can query it there.
- Every raw recording reaches a Hugging Face dataset within a day, checksummed and verified, before its local copy may be deleted.
- Nothing about the cloud copy can slow down, block or change a live run.

## 2. What changed from the controller's proposal, and why

Research (section 3) and the volume estimate (section 8) changed six points. Each one is flagged for the user.

| # | Proposal | This draft | Why |
|---|---|---|---|
| 1 | Sync every `fills` row to Postgres | Postgres gets a **per (run, algo, venue) fill summary**. The raw fills go to Hugging Face as **one gzipped CSV per UTC day**, in the same daily commit as the recordings. | Raw fills are an estimated **280 MB/year**, and up to **510 MB/year** in the high case, against a **500 MB** free database. At 500 MB the project turns read-only. The summary plus all other tables is about **57 MB/year**. |
| 2 | Public dataset `yandouziyassine/slipstream-recordings` | **Private** by default. Going public is a separate decision (open question 1). | (a) Free *public* storage is "best-effort", and HF asks for responsible use "beyond the first few gigabytes". Recordings are about **47 GB/year**. Free *private* storage is a documented **100 GB**. (b) Coinbase's Market Data Terms restrict redistributing market data. Kraken requires permission for non-personal commercial use of public-endpoint data. See section 9.6. |
| 3 | `anon` role SELECT-only now, for the future site/API/MCP | **No `anon` or `authenticated` access yet.** RLS is on for every table. The API spec grants read access when it ships. | Least privilege: nothing reads through the Data API today. It also leaves the licensing question (section 9.6) open until someone needs public access. |
| 4 | Sync rows with `id > remote max(id)` per table | **Runs are synced as whole units** (the run plus all its children, in one transaction), chosen by a count check against the remote. The id watermark is kept only for the three tables that grow after a run finishes. | A crashed run stays `started` until the collector marks it `abandoned` two or more hours later, after newer runs have finished and synced. A `max(id)` watermark would skip it forever. |
| 5 | Sync `recordings` as is | The remote stores the **path relative to `$DATA/recordings`**, never the local absolute path. Run `error` text is **path-redacted** before upload. | The local path is `/home/<user>/slipstream-data/...`, and an `OSError` names local files. The site already redacts these for the same reason (`site/render.py`). |
| 6 | Role password typed into the Supabase SQL editor | The user pastes a **SCRAM-SHA-256 verifier** that a local helper computes. The plaintext password never leaves the PC. | Postgres warns that a plaintext password in `CREATE/ALTER ROLE` "might also be logged in the client's command history or the server log" [R12]. Postgres stores a SCRAM verifier as-is [R12]. |

Unchanged from the proposal:
- Local SQLite stays the source of truth.
- Supabase syncs hourly and only finished runs; `INSERT` + `SELECT` only; TLS with certificate verification through the IPv4 session pooler.
- Hugging Face gets one commit per day plus a manifest, verified before anything is recorded locally.
- Migration 002, the 7-day "delete only if uploaded" retention with the disk cap as the emergency override, the `$DATA/cloud.enabled` switch, the two new dependencies, and the component names.

## 3. Facts this design relies on (checked 2026-09-28)

| Fact | Source |
|---|---|
| Supabase Free: 500 MB database per project, 5 GB egress, 1 GB file storage, 2 active projects, no automatic backups. Free projects "are paused after 1 week of inactivity". | [R1] [R2] |
| At more than 500 MB, a Free project "enter[s] read-only mode". Size is `pg_database_size`: data, indexes and materialized views, not WAL. | [R3] |
| Inactivity means too little *database* activity over a week; "a few user requests to the database each day" is enough. A paused project can be restored from Studio within a 1-year window. | [R4] |
| Direct connections are IPv6-only (IPv4 is a paid add-on). The **session pooler** (Supavisor) supports IPv4 at `aws-<n>-<region>.pooler.supabase.com:5432`, user `<role>.<project-ref>`. Copy the host from the Connect dialog; it cannot be derived from the region. | [R5] |
| SSL enforcement is a dashboard toggle and covers Supavisor. The CA certificate (`prod-ca-2021.crt`) downloads from Database Settings → SSL Configuration. `verify-full` is the recommended mode. | [R6] |
| Open issue (filed 2026-09-24): the pooler's intermediate CA lacks a Key Usage extension, so it **fails *strict* X.509 checks** (Python 3.13's `VERIFY_X509_STRICT`, OpenSSL strict mode). libpq's normal `verify-full` is not reported broken. We use psycopg, whose TLS is libpq's; the first manual check (plan T12) confirms it. | [R7] |
| New tables in `public` have been granted to `anon`, `authenticated` and `service_role` by default. New projects stopped doing this on 2026-05-30, and existing projects stop on 2026-10-30. Grants and RLS are separate layers. | [R8] [R9] |
| `INSERT … ON CONFLICT DO NOTHING` needs `INSERT`, plus `SELECT` on the conflict-target columns. It needs no `UPDATE`. | [R13] |
| HF storage: Free public storage is "Best-effort"; free private storage is 100 GB. Repo guidance: fewer than 100k files, fewer than 10k entries per folder, fewer than 100 files per commit. There is no hard commit limit, but the Hub "starts to degrade after a few thousand commits". | [R10] |
| HF rate limits (Free user): 1,000 API calls per 5-minute window. Commit rate limits exist but are undocumented. | [R11] |
| HF fine-grained tokens can be scoped to specific repositories. HF recommends one token per app and fine-grained tokens in production. | [R14] |
| `huggingface_hub` 2.0.0: `HfApi.create_commit(repo_id, operations, commit_message, repo_type=, parent_commit=)` "fail[s] if `revision` does not point to `parent_commit`", and `CommitOperationAdd(path_in_repo, path_or_fileobj)`. `HfApi.get_paths_info(repo_id, paths, revision=, repo_type=)` returns `RepoFile(path, size, blob_id, lfs: BlobLfsInfo(size, sha256, pointer_size) or None, xet_hash)`. Missing paths are ignored, not raised. | [R15] [R16] |
| Current versions: `huggingface_hub` 2.0.0 (pulls in `hf-xet`, `httpx2`, `click`, `fsspec`, `pyyaml`, `tqdm`, `filelock`, `packaging`, `typing-extensions`); `psycopg` / `psycopg-binary` 3.3.6. | PyPI JSON API |

## 4. Shape

```
hourly (Windows Task Scheduler → WSL → scripts/collect_hourly.sh), unchanged up to step 5:
  1-4  collect (engine + runs, :05 to about :27), then housekeeping (retention, backup, disk cap)
  5    site build + publish                                   (best-effort, as today)
  6    python -m slipstream.cloud sync      nice 10, ionice idle, timeout 5 min   (best-effort)
       └─ SQLite (read) ──TLS verify-full──► Supabase session pooler ──► Postgres schema `slipstream`
  7    python -m slipstream.cloud archive   nice 10, ionice idle, timeout 25 min  (best-effort)
       └─ does nothing unless a finished UTC day is not archived yet (normally the first run after 00:00)
          SQLite (read) + $DATA/recordings ──HTTPS──► HF dataset (private), one commit per UTC day
          verify via paths-info ──► SQLite: archive_days + recording_uploads (append-only)
```

- **Off by default.** Steps 6 and 7 return at once, with a log line, unless `$DATA/cloud.enabled` exists. The user creates that file after the setup in section 11.
- **Never in the live path.** Both steps run after the hour's runs and housekeeping, at low CPU and I/O priority, and under hard `timeout`s. The next collection (:05) always starts on an idle machine. Their failures are logged and never change the collector's exit status.
- **Own lock.** `$DATA/cloud.lock` is non-blocking. If a previous cloud step is still running, this one logs "skipped" and exits 0. It never takes `collect.lock`, so it can never make the collector skip an hour.

## 5. Supabase

### 5.1 Schema (`cloud/supabase/001_schema.sql`, pasted once by the user)

Everything lives in a dedicated schema, `slipstream`. It is not in the Data API's exposed schemas, so PostgREST cannot reach it unless someone adds it on purpose.

| Table | Columns | Source |
|---|---|---|
| `runs` | `id bigint pk`, `started_at timestamptz`, `ended_at timestamptz`, `status text check (completed/failed/abandoned)`, `side`, `qty double precision`, `duration_s int`, `fees jsonb`, `venue_rules jsonb`, `git_commit`, `error` (path-redacted), `supersedes_id bigint references runs` | local `runs`, terminal rows only |
| `results` | same columns as local, `venue_costs jsonb`, `run_id references runs` | local `results` |
| `engine_stats` | same as local | local `engine_stats` |
| `recordings` | `id`, `run_id`, `rel_path` (relative to `$DATA/recordings`, for example `2026-09-28/10-0p01.jsonl.gz`), `sha256`, `recorded_at` | local `recordings` |
| `fill_summaries` | `run_id`, `algo`, `venue`, `fills_count`, `qty`, `notional`, `fees`, `first_ts_ns`, `last_ts_ns`; pk `(run_id, algo, venue)` | computed from local `fills` at sync time |
| `recording_deletions` | same as local | local |
| `archive_days` | same as local migration 002 (section 7.1) | local |
| `recording_uploads` | same as local migration 002 | local |

- **Remote ids are the local ids.** They are explicit `bigint` primary keys with no sequences, so any row traces back to its local row.
- **CHECK constraints** mirror SQLite's: `side`, `status`, and `sha256` as 64 lowercase hex.
- **Append-only, as locally.** A `BEFORE UPDATE OR DELETE … FOR EACH ROW` trigger and a `BEFORE TRUNCATE … FOR EACH STATEMENT` trigger on every table raise `append-only`. This also guards against accidental edits in the dashboard's table editor, which runs as the owner.
- `fill_summaries` is derived, not a new source of truth. A future API can answer "how was each order split across venues" from it (the site's venue split is `SUM(qty) … GROUP BY venue`). The raw fills live on Hugging Face (section 6).

### 5.2 Role and access

```sql
create role slipstream_collector with login noinherit nocreatedb nocreaterole nobypassrls;  -- no password yet
revoke all on schema slipstream from public;
grant usage on schema slipstream to slipstream_collector;
-- for every table T:
revoke all on slipstream.T from public, anon, authenticated;
grant select, insert on slipstream.T to slipstream_collector;
alter table slipstream.T enable row level security;
create policy collector_read   on slipstream.T for select to slipstream_collector using (true);
create policy collector_insert on slipstream.T for insert to slipstream_collector with check (true);
alter role slipstream_collector set statement_timeout = '60s';
```

- **No `UPDATE`, `DELETE` or `TRUNCATE`,** and no sequence or function grants. That is enough because only finished, immutable runs sync (section 5.3).
- The `revoke … from anon, authenticated` is explicit, so the script is correct on projects created before and after 2026-05-30 [R9].
- **The password is set separately (T5, T12).** The user generates a random password (`secrets.token_urlsafe(32)`) and puts it in `.env`. `python -m slipstream.cloud scram-verifier` prints `ALTER ROLE slipstream_collector PASSWORD 'SCRAM-SHA-256$4096:<salt>$<StoredKey>:<ServerKey>';` and the user pastes that line into the SQL editor. Postgres stores a SCRAM verifier as-is [R12], so the plaintext never reaches Supabase's editor history or server log. Supavisor authenticates with the stored SCRAM secret.

### 5.3 Sync algorithm (`python/slipstream/cloud/supabase_sync.py`)

Each pass is one connection. Every statement uses parameters, and table names come from a fixed tuple.

1. **Capacity guard.** Read `pg_database_size(current_database())`. At 400 MB or more, log a warning. At 450 MB or more, log an error, skip the pass and return nonzero; section 10 says what the user does. The size is logged every hour, so the trend stays visible.
2. **Runs, as whole units.**
   - Remote: `select count(*), coalesce(max(id), 0) from slipstream.runs` gives `(n, m)`. Local: count the runs with `id <= m` and `status <> 'started'`, giving `k`.
   - If `k == n`, the missing runs are the local terminal runs with `id > m`. This is the usual case, with O(1) remote reads, because the remote only ever receives local terminal runs and local rows are never deleted.
   - If `k > n`, a late-closing run (for example one marked `abandoned`) sits below `m`. Fetch the remote ids and send the difference. This is rare.
   - If `k < n`, the remote holds runs that are unknown or unfinished locally, for example after the local DB was restored from an older backup. Log an error and stop the pass. Never guess.
   - Send at most 500 runs per pass, oldest first. Each run is one transaction: the `runs` row, then its `results`, `engine_stats`, `recordings` (relative path) and `fill_summaries`, each `ON CONFLICT DO NOTHING`.
   - This is complete because every child row of a run is written before `finish_run` (`collect._run_size`), and none are added after.
3. **Tables that grow after a run has finished.** `recording_deletions`, then `archive_days`, then `recording_uploads`, in that order, parents first. For each, insert the local rows with `id > remote max(id)`, in id order, in one transaction per table, `ON CONFLICT DO NOTHING`. A watermark is correct here because each table is written by one process in id order and its parent run has already synced. A foreign-key error (a parent not synced yet) rolls back that table's transaction and is retried next hour.
4. **Outbound values.**
   - The local path `P` becomes `P.relative_to($DATA/recordings)`. It must resolve inside that directory; otherwise the run is skipped and logged.
   - `error` goes through `redaction.redact_paths`.
   - JSON text columns are parsed locally and sent as `jsonb`. A parse failure skips the run and is logged; that is a local bug, never guessed around.
5. **Inbound values** (counts, max ids, id lists, size) are checked to be `int`s of 0 or more. Anything else ends the pass as an error.

The connection uses `host` (the validated pooler host), `port=5432`, `dbname=postgres`, `user=slipstream_collector.<project-ref>`, `sslmode=verify-full`, `sslrootcert=$DATA/cloud/supabase-ca.crt`, `connect_timeout=10`, and `application_name=slipstream-collector`. If the CA file is missing, the pass refuses to connect. It never falls back to a weaker `sslmode`.

## 6. Hugging Face archive (`python/slipstream/cloud/hf_archive.py`, `hub.py`)

### 6.1 Dataset layout (`yandouziyassine/slipstream-recordings`, private)

```
README.md                                        dataset card (user pastes cloud/hf/README.md once)
recordings/2026/09/27/10-0p01.jsonl.gz           the local file, byte-for-byte (≤ 48 per day folder)
fills/2026/09/27.csv.gz                          every fill of every run started that UTC day
manifests/2026/09/27.jsonl                       one line per file in that day's commit
```

- **Manifest line:** `{"kind": "recording", "run_id": 123, "path": "recordings/…", "sha256": "…", "bytes": 2831044, "recorded_at": "2026-09-27T10:16:02"}`, or `{"kind": "fills", "path": "fills/…", "sha256": "…", "bytes": …, "rows": …}`. The manifest is append-only by construction: one new, never-rewritten file per day.
- **Fills CSV columns:** `fill_id, run_id, algo, venue, qty, price, fee, ts_ns`, ordered by `fill_id`. Floats are written with `repr`.
- **Deterministic by design.** The gzip uses `mtime=0` and an empty file name, and the manifest has sorted keys and a fixed line order. The same local DB always produces the same bytes. That is what makes the reconcile step (6.2, step 3) safe after a crash.
- **Paths are built only from validated parts:**
  - the run's UTC date, parsed with `strptime`;
  - the hour;
  - the qty token, matched against `^[0-9]+(p[0-9]+)?$`.
  - A DB path string is never pasted into a repo path.

### 6.2 Daily pass

A UTC day `D` is **ready** when `D` is before today (UTC), `D` has at least one run, no run started on `D` is still `started`, and `D` is not in `archive_days`. The ready days are processed oldest first, one commit each. The pass stops starting new days after 20 minutes; the `timeout 25m` in the script is the hard stop.

For each ready day:
1. **Collect.**
   - Take the day's recordings that are not deleted.
   - Each local file must exist, resolve inside `$DATA/recordings`, and match the SHA-256 in `recordings`. A file that does not is left out of this commit and logged as an error, and the day still archives. A missing or corrupt file is reported, never uploaded.
   - Build the fills CSV and the manifest in `$DATA/cloud/staging/<D>/`.
2. **Read the remote head.** Get the head commit id, and `get_paths_info` for every planned path at that head.
3. **Reconcile.**
   - A planned path that already exists remotely with the same size and SHA-256 is not uploaded again. This is how a crash after a commit, but before the local record, is healed.
   - A planned path that exists with *different* content aborts the day with an error. **Remote evidence is never overwritten.**
4. **Commit.** `create_commit(..., parent_commit=head)` with the remaining files, at most 97 per commit (48 recordings + fills + manifest is normally 50). The message is `archive <D>: <n> recordings, <m> fills`. `parent_commit` makes a second writer, such as a future VM, fail instead of racing.
5. **Verify.**
   - Call `get_paths_info(paths, revision=<new commit oid>)`.
   - For a file stored through LFS or Xet, `size` and `lfs.sha256` must equal the local values.
   - For a small file stored in git (the manifest), `blob_id` must equal the git blob SHA-1 computed locally.
   - Any mismatch or missing path marks the day as failed; nothing is recorded, and it is retried tomorrow.
6. **Record locally** only after verification, in one SQLite transaction: an `archive_days` row plus one `recording_uploads` row per recording.
7. Remove `$DATA/cloud/staging/<D>/`.

Normal cost is one commit a day (365 a year), 50 files, about 130 MB. A backlog (the PC was off, or HF failed) catches up at one day per commit, as many as fit in 20 minutes.

### 6.3 `hub.py`: the only file that imports `huggingface_hub`

```python
@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int
    sha256: str | None   # lfs.sha256, None for a plain git blob
    blob_id: str

class HubError(RuntimeError): ...  # message already redacted

class HubClient(Protocol):
    def head(self) -> str: ...
    def paths_info(self, paths: Sequence[str], revision: str) -> dict[str, RemoteFile]: ...
    def commit(self, files: Sequence[tuple[str, Path]], message: str, parent: str) -> str: ...
```

`HfHubClient(token, repo_id)` implements this over `HfApi`:
- It always passes `repo_type="dataset"` and `token=` explicitly. It never calls `login()`, never reads a cached token, and never sets `HF_TOKEN` in the environment.
- It sets `HF_HUB_DISABLE_TELEMETRY=1`.
- It turns every `huggingface_hub` exception into a `HubError` holding a redacted summary: the exception type and HTTP status, with the token removed.
- The rest of the code and all tests use the protocol; tests use a fake.

## 7. Local changes

### 7.1 Migration `002_cloud_archive.sql`

```sql
CREATE TABLE archive_days (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL UNIQUE,            -- YYYY-MM-DD, UTC
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
```

- Both tables get the append-only `BEFORE UPDATE` and `BEFORE DELETE` triggers, and the migration adds `schema_version` row 2.
- `archive_days` is needed as well as the proposed `recording_uploads`: it records the day's commit, manifest and fills file, and "a day is archived" must be a fact of its own. A day in which every run failed still has fills (none) and a manifest.
- `UNIQUE(run_id)` makes a double upload impossible to record.

### 7.2 Retention (`storage.py`, `collect.py`)

When `$DATA/cloud.enabled` exists:
- **Daily (hour 0):** delete every recording older than `full_retention_days` (7) that has a `recording_uploads` row. Each deletion is recorded in `recording_deletions` before the file is unlinked, as today.
  - Thinning to the 12:00 run, and the 90-day expiry, are skipped. Hugging Face holds the full history, so keeping the noon copy locally no longer adds anything.
  - Recordings older than 7 days that are **not** uploaded are kept, and counted in a warning (`recordings_awaiting_upload`).
- **Disk cap (every hour, unchanged cap):** it evicts **uploaded recordings first**, oldest first, then non-uploaded ones, oldest first, and never this hour's.
  - Evicting a non-uploaded recording is logged as a warning, `recordings_evicted_unarchived`, with its count. That is real data loss and must be visible.
  - The eviction is still recorded in `recording_deletions`, which syncs to Supabase.

When the flag is absent, retention is exactly today's.

At about 130 MB/day, 7 days is about 0.9 GB, close to the 1 GB default cap. In practice the cap will evict the oldest *uploaded* recordings slightly before day 7, which is harmless. Recordings waiting for upload are normally at most about 2 days old (about 260 MB), well under the cap.

## 8. Volume estimate

**Workload:**
- `CollectConfig`: 2 runs/hour (0.01 and 0.25 BTC), each with 4 algorithms, 10 slices, POV at 0.1. That is **17,520 runs/year** and **70,080 results/year**.
- **Fills per run:**
  - The engine emits one fill per venue leg per child (`engine.cpp` `advance_locked`).
  - Measured live (README, 2026-09-26, 0.02 BTC, 10 slices): 39–43 fills per run (about 10 per sliced algo, 9–13 for POV).
  - POV sends a child whenever market volume rises past the smallest venue minimum. For 0.25 BTC at 10% participation it needs about 2.5 BTC of traded volume, estimated at **50–250 fills**; this is not measured yet.
  - Estimate: 0.01 BTC runs about 40 fills, 0.25 BTC runs about 80–280. That gives **~110 fills/run typical and ~200 high**.

**Postgres bytes per row:** a 24-byte tuple header plus a 4-byte line pointer, 8-byte aligned values, plus about 22 bytes per btree index entry. These are estimates to within ±30%. The first real week replaces them with the logged `pg_database_size`.

| Table (Postgres) | Rows/year | Bytes/row incl. indexes | MB/year |
|---|---|---|---|
| `runs` (jsonb fees and venue rules about 240 B) | 17,520 | ~400 | 7.0 |
| `results` (jsonb venue costs about 120 B) | 70,080 | ~310 | 21.7 |
| `engine_stats` | 17,520 | ~110 | 1.9 |
| `recordings` | 17,520 | ~195 | 3.4 |
| `recording_deletions` | ≤ 17,520 | ~75 | 1.3 |
| `recording_uploads` | 17,520 | ~275 | 4.8 |
| `archive_days` | 365 | ~450 | 0.2 |
| `fill_summaries` (≤ 8, about 6 per run) | ~105,000 | ~150 | 15.8 |
| **Total (this design)** | | | **≈ 57 MB/year** |
| *Rejected: raw `fills`* | 1.93 M typical / 3.50 M high | ~145 | *280 / 510* |

- **This design fits:** about 7 years before the 450 MB guard, minus Supabase's own baseline, which the user reads once after setup (plan T12).
- **Syncing raw fills does not fit:** year one alone reaches about 340 MB typical and about 570 MB high. The high case goes read-only within the first year.

**Hugging Face:**
- Recordings: 2.7 MB (gzip-9) per 10-minute recording. PR #26 measured this from about 120 s of live data and extrapolated; it will vary with market activity. That is about **130 MB/day, about 47 GB/year.**
- Fills CSV: about 5,300 fills/day, about 120 KB gzipped, about 44 MB/year. Manifests are about 10 KB/day.
- **Files:** about 18,250/year. The 100k-file guidance is reached after about 5.5 years. Folders hold at most 48 entries (day folders) or 31 (month folders).
- **Commits:** 365/year, so "a few thousand" is 5–8 years away.
- **Private 100 GB quota:** full after about **2.1 years** (open question 5).

## 9. Security

1. **Paper only, outbound only.** The two new connections are TLS to Supabase's pooler (Postgres wire protocol) and HTTPS to huggingface.co. There is no listening socket. The engine is untouched.
2. **Secrets.**
   - Only in the repo's `.env` (gitignored), which only the user edits. The assistant never reads, writes or types them.
   - Keys: `SLIPSTREAM_SUPABASE_POOLER_HOST`, `SLIPSTREAM_SUPABASE_PROJECT_REF`, `SLIPSTREAM_SUPABASE_COLLECTOR_PASSWORD`, `SLIPSTREAM_HF_TOKEN`.
   - `cloud/settings.py` parses `KEY=VALUE` lines itself (no new dependency, no shell `source`). It reads only these four keys and ignores any others.
   - Each value is validated, and an error names the key, never the value:
     - host `^aws-[0-9]+-[a-z0-9-]+\.pooler\.supabase\.com$`, so the password can only ever be sent to a Supabase pooler;
     - project ref `^[a-z0-9]{20}$`;
     - password: ASCII, at least 24 characters;
     - token `^hf_[A-Za-z0-9]{30,}$`.
   - Secret fields use `repr=False`. Secrets are never put into a child process's environment.
3. **Logs.**
   - Cloud modules log a summary: the exception type, psycopg's `sqlstate` or the HTTP status, and a message passed through `redaction.redact(text, secrets)`. That replaces every secret value with `<redacted>`, redacts local paths, and truncates to 300 characters.
   - They never log with `exc_info`.
   - Deletion and upload log lines give counts, days and MB, never local paths. This matches `publish.py` and `storage.py`.
4. **Least privilege.**
   - The Postgres role can `SELECT`/`INSERT` only, in one schema, with RLS on, no `BYPASSRLS` and a 60 s statement timeout.
   - The HF token is fine-grained, with write access to the contents of `yandouziyassine/slipstream-recordings` and nothing else, one token for this PC.
   - The `postgres` owner password stays in the user's password manager and is never in `.env`.
   - **Residual risk:**
     - `.env` sits on the Windows drive (`/mnt/c`), where any process running as the user can read it. This is the same exposure as the existing deploy key.
     - A stolen HF token can rewrite the dataset's history. If that happens it is detectable: every uploaded file's SHA-256 is also in `recording_uploads`, in both the local DB and Supabase, so a changed file no longer matches. If either secret leaks, the user rotates it (a new token, or a new verifier via `scram-verifier`).
5. **What is uploaded, and only that.**
   - Files are uploaded only from `$DATA/recordings` (after resolving symlinks) and from `$DATA/cloud/staging`.
   - Repo paths are built from validated parts. Local absolute paths never leave the machine (the relative `rel_path` and redacted `error` are tested).
   - **Recordings contain only public exchange messages.** This was checked against `recorder.py` and `collect.py`:
     - `record_stream` writes one line per message *received* from `wss://ws.kraken.com/v2` (channels `book`, `trade`) and `wss://advanced-trade-ws.coinbase.com` (channels `level2`, `market_trades`, `heartbeats`): `{"recv_ns": <local clock ns>, "venue": …, "msg": <message as received>}` (`_encode`).
     - Our outgoing subscribe messages are not written, and they carry no credentials (`kraken.subscribe_message`, `coinbase.subscribe_messages`).
     - Every message passes the venue parser before it is written.
     - The collector never writes the OHLC header (`write_ohlc_header` is unused by `collect.py`).
     - The gzip header holds only the file's base name (for example `10-0p01.jsonl`) and a timestamp; CPython's `gzip` writes `os.path.basename`.
     - Exchange acks and status messages carry connection-level values (for example Kraken's `connection_id`), not user data. `recv_ns` reveals the collector's clock timing, which is the point of recording.
6. **Licensing: an open risk the user must decide on. This is not legal advice.**
   - "Public" means unauthenticated, not freely redistributable.
   - Coinbase's Market Data Terms (last updated 2026-08-06) say that without prior written consent you may not "redistribute, display, or disseminate the Market Data" or "Derived Works" (including "data, charts, analytics, research") to third parties [R17]. The terms are presented for the Exchange Market Data API. Whether they bind the Advanced Trade public WebSocket could not be confirmed: the Developer Platform terms page returned HTTP 403 to automated fetches.
   - Kraken: "You must seek our prior permission for … any non-personal commercial use of data from publicly accessible endpoints, such as market data" (contact `marketdata@kraken.com`) [R18].
   - This is why the dataset is private and `anon` has no access in this draft.
   - **The research page (built, but never published: `slipstream-live` does not exist and publishing is off) would be a Derived Work in Coinbase's terms, so this question must be answered before its first publish too** (open question 2).
7. **Validation at the boundaries.** `.env` values (above); remote counts, ids and sizes (section 5.3, step 5); remote file metadata (section 6.2, step 5); local files re-hashed before upload.
8. **Supply chain.**
   - Two direct dependencies, hash-pinned through pip-compile and covered by `pip-audit` in CI.
   - `huggingface_hub` brings about 9 transitive packages, including the native `hf-xet`. That is the cost of not re-implementing the Hub's LFS/Xet upload protocol, which would be riskier.
   - `psycopg[binary]` ships its own libpq and OpenSSL.

## 10. Failures

| Failure | Behaviour |
|---|---|
| No network, DNS, TLS or auth error | Log a redacted summary and return nonzero. The next hour retries the sync and the next run retries the archive. Collection is unaffected. |
| Supabase project paused (a week with the PC off) | Connection errors are logged every hour. The user restores the project in Studio (1-year window [R4]), and the next pass sends the backlog. |
| Supabase database at 450 MB or more | The sync skips itself, logs `supabase_near_limit` every hour, and the controller adds an `input.md` entry. Local data is complete, so nothing is lost. |
| HF 429 / quota / 5xx | The day is not recorded, and it is retried at the next run. Recordings stay local (awaiting upload) under the disk cap. |
| Commit landed but the local record failed (crash or timeout) | Healed by the reconcile step: same bytes, same SHA-256, so nothing is recommitted. |
| Remote file differs from the local one | The day aborts with an error. Never overwritten. Needs a human. |
| Local recording missing or corrupt | Left out of the commit and logged. The manifest lists only what was uploaded, and the deletion (if any) is in `recording_deletions`. |
| Disk cap must evict a non-uploaded recording | Evicted (the disk wins), and logged as the warning `recordings_evicted_unarchived`. |
| `.env` value missing or invalid | That service logs `cloud_config_invalid: <KEY>` and is skipped. The other service still runs. |

## 11. User setup (summary; exact steps are in plan T12 and go into `input.md`)

1. **Supabase:**
   - a free account and a Free-plan project;
   - enforce SSL and download the CA to `~/slipstream-data/cloud/supabase-ca.crt`;
   - paste `cloud/supabase/001_schema.sql` into the SQL editor;
   - put the pooler host, project ref and a generated collector password in `.env`;
   - paste the verifier line printed by `python -m slipstream.cloud scram-verifier`.
2. **Hugging Face:**
   - a free account;
   - a private dataset `slipstream-recordings`, with the card from `cloud/hf/README.md`;
   - a fine-grained token (write on that dataset only), put in `.env`.
3. `python -m slipstream.cloud check`: TLS `verify-full` to the pooler, `select 1` as the collector role, the dataset head readable with the token. It prints OK or FAIL per service, with no secrets.
4. `touch ~/slipstream-data/cloud.enabled`.

## 12. Testing

- **Postgres (real, local):**
  - A pytest fixture runs a throwaway cluster (`initdb` in `tmp_path`, Unix socket only, no TCP).
  - It creates stub `anon` and `authenticated` roles, then applies `cloud/supabase/001_schema.sql` unchanged.
  - Checks:
    - the collector role can `INSERT` and `SELECT`;
    - `UPDATE`, `DELETE` and `TRUNCATE` are refused (both by privilege and by the triggers, as the owner);
    - `anon` can read nothing.
  - CI requires this fixture: `SLIPSTREAM_REQUIRE_PG=1` turns a missing `initdb` into a failure instead of a skip.
- **Sync:**
  - end-to-end from a `ResultsDB` in `tmp_path` into the fixture: a second pass inserts nothing;
  - an abandoned run that closes after newer runs are synced still arrives;
  - `k < n` stops the pass;
  - no local absolute path or home directory appears in any remote row (a full-table scan for `str(Path.home())` and `$DATA`);
  - the capacity guard uses an injected size;
  - a secret in an exception's text never reaches the log;
  - the connection kwargs always hold `sslmode=verify-full` and `sslrootcert`, and a missing CA refuses to connect.
- **SCRAM helper:** a verifier made by `scram.py` is set on a fixture role with `pg_hba` forced to `scram-sha-256`, and a login with the plaintext succeeds.
- **Archive (fake `HubClient`):**
  - one commit per ready day, and none for today or for a day with a `started` run;
  - the manifest and fills CSV are byte-identical on rebuild;
  - reconcile skips identical remote files and aborts on a differing one;
  - a verification mismatch records nothing;
  - a DB path pointing outside `$DATA/recordings` (for example `~/.ssh/id_ed25519`) is never uploaded;
  - a corrupt local file is left out;
  - at most 97 files per commit;
  - the deadline stops new days;
  - the token never appears in logs.
- **`hub.py`:** against a stub `HfApi`, it passes `repo_type="dataset"`, `parent_commit` and `revision=<new oid>`, maps `lfs`/`blob_id`, and maps exceptions to `HubError` without the token.
- **Retention:**
  - never deletes a non-uploaded recording before the cap forces it;
  - deletes uploaded ones after 7 days;
  - the cap evicts uploaded first and logs unarchived evictions;
  - with the flag absent, behaviour is unchanged (the existing tests stay green).
- **Settings:** each key's validation; unknown keys are ignored; error text never contains a value; `repr` hides secrets.
- **Manual (with the user):**
  1. `check`;
  2. one real `sync`, then compare row counts in the dashboard;
  3. one real `archive` of a finished day, then compare the manifest to the local SHA-256s;
  4. the Supabase baseline size is noted in `note.md`.

## 13. Open questions for the user

1. **Dataset visibility:** keep it private (recommended: licensing risk, and a documented 100 GB quota) or public (best-effort storage, and it needs the answer to question 2)?
2. **Licensing:** should the controller draft an email to `marketdata@kraken.com` and to Coinbase asking permission for non-commercial research use of raw and derived data? This also covers the research page, which is not published yet and should stay unpublished until this is answered.
3. **Fills:** accept the summary in Postgres plus a daily CSV on HF (recommended)? The smaller alternative puts only the summary in Postgres and keeps raw fills local and in local backups, with no off-site raw copy.
4. **`anon` access:** OK to grant it only when the API spec ships?
5. **At about 2 years (private quota full):** decide then between (a) making the dataset public, (b) archiving only the 12:00 recordings from then on (about 2 GB/year), or (c) squashing old history (destructive). Until then the archive logs an error when HF refuses a commit, and the recordings stay local under the cap.
6. **Supabase region:** the region nearest the PC (and the future Oracle VM), chosen at project creation.
7. **One flag or two:** a single `cloud.enabled` for both services (as proposed), or `supabase.enabled` and `hf.enabled` so each can be enabled on its own?

## 14. Out of scope

- The public API/MCP read path, and any `anon`/Data API grants.
- Moving collection to the Oracle VM. The same code runs there after moving `.env` and the CA file; `parent_commit` prevents two writers from racing.
- Restoring a local DB from the cloud (possible from Supabase, HF fills and HF recordings, but not built).
- Re-encoding recordings as Parquet for the HF dataset viewer.

## References

- [R1] https://supabase.com/pricing
- [R2] https://supabase.com/docs/guides/platform/billing-on-supabase
- [R3] https://supabase.com/docs/guides/platform/database-size
- [R4] https://supabase.com/docs/guides/platform/free-project-pausing
- [R5] https://supabase.com/docs/guides/database/connecting-to-postgres
- [R6] https://supabase.com/docs/guides/platform/ssl-enforcement
- [R7] https://github.com/supabase/supabase/issues/50862
- [R8] https://supabase.com/docs/guides/database/postgres/row-level-security
- [R9] https://supabase.com/changelog/45329-breaking-change-tables-not-exposed-to-data-and-graphql-api-automatically
- [R10] https://huggingface.co/docs/hub/storage-limits
- [R11] https://huggingface.co/docs/hub/rate-limits
- [R12] https://www.postgresql.org/docs/current/sql-createrole.html
- [R13] https://www.postgresql.org/docs/current/sql-insert.html
- [R14] https://huggingface.co/docs/hub/security-tokens
- [R15] https://huggingface.co/docs/huggingface_hub/main/en/package_reference/hf_api
- [R16] https://github.com/huggingface/huggingface_hub/blob/main/src/huggingface_hub/hf_api.py (`RepoFile`, `BlobLfsInfo`, `get_paths_info`)
- [R17] https://www.coinbase.com/legal/market_data (the page returns HTTP 403 to automated fetches; quoted from its indexed text, "last updated August 6, 2026")
- [R18] https://docs-legacy.kraken.com/api/docs/guides/global-intro/
