# Hourly Collector, Results Database and Live Research Page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect hourly paper-trading evidence into an append-only SQLite database, starting now on the user's PC. Publish an honest research page built from that data to GitHub Pages.

**Spec:** `docs/superpowers/specs/2026-09-27-collector-and-leaderboard-design.md` (approved 2026-09-27). Read it first.

**Tech stack:**
- Python 3.12, standard library only for new code:
  - `sqlite3`, `gzip`, `fcntl`, `html`, `string`, `random`, `statistics`, `subprocess`
  - no new dependencies
- The existing `LiveSession`, `record_stream`, `venue-flags` and release engine
- Bash and PowerShell (Windows Task Scheduler)

**Rules for every task (CLAUDE.md, plus the efficiency rules from memory):**
- **TDD:** red, then green, then refactor.
- **Tests:** run only the relevant tests while working. Run the full `bash scripts/ci.sh` once per PR.
- **Reviews:** the implementer does its own security and anomaly self-review. Then one whole-PR `/security-review` and `/caveman:caveman-review` before the push.
- **Commits:**
  - Use the noreply author and committer through env vars (never through git config).
  - Add a `Co-Authored-By` trailer.
  - Run git from Windows.
- **Commands:** WSL through scratchpad scripts, `MSYS_NO_PATHCONV=1 wsl.exe bash /mnt/c/...`.
- **Data location:** all runtime data goes under `SLIPSTREAM_DATA_DIR`, default `~/slipstream-data` on the WSL ext4 filesystem, never `/mnt/c`. Tests use `tmp_path`.

---

## PR A — Collector and database (`feat/collector`)

### A1. Results database: `python/slipstream/db/`

**Files:**
- `db/__init__.py`
- `db/migrations/001_initial.sql`
- `db/results_db.py`
- `python/tests/test_results_db.py`

**Migration `001_initial.sql`:** the tables from spec §4.

Each table has `BEFORE UPDATE` and `BEFORE DELETE` triggers that `RAISE(ABORT, 'append-only')`. The one exception is `runs`: an update may move `status` from `'started'` to a terminal value, and may set `ended_at` and `error`, with every other column unchanged. That exception is written as a `WHEN` condition that compares `OLD.col IS NEW.col` for every other column.

`schema_version` gets a row with `1`.

**`ResultsDB(path)`:**
- On open, sets `PRAGMA journal_mode=WAL; foreign_keys=ON; busy_timeout=5000`.
- `migrate()` applies the `*.sql` files it has not applied yet, in order, each in a transaction.
- Every statement uses parameters. There is no string formatting of values.

| Method | What it does |
|---|---|
| `begin_run(started_at, side, qty, duration_s, fees, venue_rules, git_commit) -> int` | Inserts the run and returns its id |
| `finish_run(run_id, status, ended_at, error=None)` | `status` must be completed or failed |
| `add_results(run_id, statuses, fills, stats)` | Takes the `pb.OrderStatus` list, the `models.Fill` list with the algo resolved from the order id, and engine stats |
| `set_recording(run_id, path, sha256)` | Uses a separate `recordings` row or column, since `runs` is append-only (see note) |
| `mark_abandoned(now) -> int` | Marks runs still `started` that are older than 2 h as `abandoned`, and returns the count |
| `thin_recordings(now, keep_hour_utc=12) -> list[Path]` | Returns the files to delete. The caller deletes them after the DB change commits. |
| `backup(dest: Path)` | `sqlite3.Connection.backup` |

**Note:** because `runs` is append-only, the recording and its thinning live in a separate append-only `recordings(run_id, path, sha256, recorded_at)` table plus a `recording_deletions(run_id, deleted_at)` table. Add both to the migration and to the spec's table list.

**Tests:**
- Migrations apply to an empty file, and a second `migrate()` is a no-op.
- `UPDATE` or `DELETE` on `results`, `fills`, `engine_stats` and `recordings` raises `sqlite3.IntegrityError` (the abort).
- A run can move from `started` to `completed` exactly once. Changing `side` in that update is refused. A second finish is refused.
- A failed run keeps its error.
- `mark_abandoned` only touches runs that are old and still started.
- Thinning keeps the 12:00 recording each day, lists the others older than 30 days, and records deletions.
- A backup opens and has the same row counts.
- A hostile string (quotes and `;DROP TABLE`) round-trips as data.

### A2. Hourly collection: `python/slipstream/collect.py`

**Files:** `collect.py` and `python/tests/test_collect.py`.

- `CollectConfig`:
  - `sizes=(0.01, 0.25)`, `duration_s=600`, `slices=10`, `participation=0.1`
  - `venues=("kraken","coinbase")`, `symbol="BTC/USD"`
  - `data_dir` from `SLIPSTREAM_DATA_DIR`, default `~/slipstream-data`
- `side_for(hour_utc)`: `"buy"` for even hours, `"sell"` for odd.
- `acquire_lock(data_dir) -> contextmanager | None` uses a non-blocking `fcntl.flock`. It returns None when another run holds the lock.
- `async run_hour(engine_address, cfg, now, git_commit, db, urls=None) -> list[int]`:
  - For each size, one after the other:
    1. `begin_run`.
    2. Start `record_stream` for the raw recording. The target is `$DATA/recordings/<date>/<hour>-<size>.jsonl.gz`, written through `gzip.open` in text mode. It runs concurrently with a `LiveSession` over all four algorithms, with the order ids `h<YYYYMMDDHH>-<size>-<algo>`.
    3. `add_results`.
    4. `set_recording` with the SHA-256.
    5. `finish_run(completed)`.
  - Any exception: `finish_run(failed, error=str(exc)[:500])`, then continue to the next size. It never raises out, except for a DB failure.
  - Venue rules and fees come from the engine (`venue_fees()`) and `fetch_venue_rules`, which are stored as JSON.
- `main()` for `python -m slipstream.collect run`:
  1. Resolve the data dir (create it with mode 700).
  2. Take the lock (if it is held, log "skipped" and exit 0).
  3. Open and migrate the DB.
  4. `mark_abandoned`.
  5. `run_hour`.
  6. `backup` to `$DATA/backup/slipstream-<date>.db`, keeping 14.
  7. Once a day (the first run after 00:00 UTC), thin recordings and delete the listed files.
- Logs go to `$DATA/logs/collect-<date>.log` (JSON lines, through the existing logging setup).

**Tests** (fake Kraken and Coinbase `ws://` servers from `tests/test_live_session.py`, a real live-mode engine fixture):
- One `run_hour` with small sizes and a short duration (use test overrides: `duration_s=4`, `slices=2`, sizes `(0.001, 0.002)`) writes 2 completed runs with 4 results each, plus fills and stats.
- The recording file exists, is gzip, and its SHA-256 matches.
- A feed parser error makes that size `failed` with an error, and the next size still runs.
- The lock prevents a concurrent second run.
- `side_for` works on both parities.

### A3. Scripts and scheduled task

**`scripts/collect_hourly.sh`** (`set -euo pipefail`):
1. `cd` to the repo.
2. Resolve `DATA="${SLIPSTREAM_DATA_DIR:-$HOME/slipstream-data}"`.
3. `bash scripts/build_release.sh >/dev/null`.
4. Get the venue arguments through `mapfile` from `python -m slipstream.cli venue-flags --venues kraken,coinbase --fees kraken=40,coinbase=60`.
5. Start the engine on `127.0.0.1:0`, reading its port from stdout the way conftest does, with `--clock live --max-order-notional 50000 --max-position 1 "${VENUE_ARGS[@]}"`.
6. `trap` kills the engine on exit.
7. Run `python -m slipstream.collect run --engine 127.0.0.1:$PORT`.
8. Append all output to `$DATA/logs/`.
9. Exit 0 even on a failed run (the failure is recorded in the DB), except when there is no engine.

**`scripts/install_task.ps1`** takes `-Install` or `-Uninstall`:
- It registers a Windows scheduled task named `Slipstream hourly collector`:
  - trigger: hourly at :05;
  - action: `wsl.exe -e bash /mnt/c/Code/slipstream/scripts/collect_hourly.sh`;
  - it runs as the current user, only when logged on, with no stored password and no highest privileges;
  - `-MultipleInstances IgnoreNew`, with a 50-minute execution time limit.
- It prints what it did. `-Uninstall` removes the task.
- **The implementer does NOT run `-Install`.** The controller asks the user first.

**CLI:** `slipstream collect run` delegates to `collect.main`.

**Tests:**
- `bash -n` on the shell script.
- A PowerShell syntax check through `pwsh`/`powershell -NoProfile -Command "[scriptblock]::Create((Get-Content ... -Raw))"` from Windows, run by the controller.

### A4. Docs and PR
- README: add a "Hourly evidence" paragraph covering what is collected and where, and how to install or uninstall the task.
- `note.md`: add a dev log entry.
- Update the spec with the recordings tables from A1.
- Run the full CI, then the whole-PR reviews, then push and open the PR, and merge when green.
- **After the merge,** with explicit user approval in chat:
  1. Run `install_task.ps1 -Install`.
  2. Trigger one run with `Start-ScheduledTask`.
  3. Check the DB rows together with the user.

---

## PR B — Research site and publishing (`feat/research-site`)

### B1. Statistics: `python/slipstream/site/stats.py`
- `median_ci(values, n_boot=1000, seed=7, alpha=0.05) -> (median, lo, hi)` uses a bootstrap with `random.Random(seed)`.
- `diff_ci(a, b, ...)` does the same for the difference of medians.
- `verdict(a_name, a, b_name, b) -> str` returns `"<a> cheaper"`, `"<b> cheaper"` or `"not yet distinguishable (n = …)"`. It only names a winner when the interval excludes 0.
- **Tests:**
  - A hand-checked tiny sample with a fixed seed.
  - Identical samples give "not yet distinguishable".
  - Clearly separated samples name the cheaper one.
  - n is always reported.

### B2. Rendering: `python/slipstream/site/render.py`, `svg.py`, `templates/`
- **Templates** are plain HTML files with `string.Template` placeholders. Every value goes through a single `esc()` (`html.escape(str(v), quote=True)`). No raw value reaches HTML except SVG built by `svg.py` from numbers.
- **`svg.py`:**
  - `line_chart(series: dict[str, list[tuple[date, float]]], bands, width, height)` and `bar_split(rows)`.
  - Only numeric input. Labels are escaped.
- **Design (mockup v3):**
  - Paper background `#f6f4ee`, ink `#1b1b1a`, one accent (green `#1f6b47` for cheaper), hairline rules.
  - A serif for text, with monospaced tabular figures for numbers.
  - **Fonts use system stacks only** (Georgia/Charter serif, ui-monospace/Consolas mono, system-ui sans). No downloads and no third-party requests.
- **Pages:**
  - `index.html`:
    - the 3-line explainer and the GitHub link;
    - a status column;
    - KPIs;
    - a 30-day chart with bands, per size;
    - fees versus slippage;
    - the last runs table;
    - a "Use it yourself" section (API and MCP, marked *coming soon*);
    - the notes.
  - `history.html`: all runs, filtered by a small inline script (no library).
  - `methodology.html`: static text, including the known limitations.
  - `data/runs.csv`.
- **Tests:**
  - Snapshot of `index.html` from a fixed test DB (built with `ResultsDB` in `tmp_path`).
  - A hostile string in `error` or `halt_reason` shows up escaped.
  - No external URL appears in any generated page except the GitHub links (grep test).
  - Charts handle 0 and 1 data points.

### B3. Build and publish: `python/slipstream/site/__main__.py`
- `python -m slipstream.site build` writes `$DATA/site/` deterministically.
- `python -m slipstream.site publish`:
  - Returns immediately with "publishing disabled" unless `$DATA/publish.enabled` exists.
  - Otherwise it syncs `$DATA/site/` into a clone at `$DATA/publish-repo` (cloning on first use from `git@github.com:yandouziyassine/slipstream-live.git`).
  - Commits as the noreply author only when something changed.
  - Pushes with `GIT_SSH_COMMAND="ssh -i ~/.ssh/slipstream_live_deploy -o IdentitiesOnly=yes"`.
  - A push failure is logged and returns nonzero, but never raises into the collector.
  - The remote URL can be overridden only through an env var used by tests.
- **Tests** (local bare repo as the remote, no network):
  - Disabled without the flag file.
  - The first publish creates a commit.
  - A second publish with no changes creates no commit.
  - The key path is never printed.
- **`collect_hourly.sh`:** after collection, run `site build` then `site publish`. Both are failure-tolerant.

### B4. User steps (`input.md`) and PR

Write the user steps in `input.md`:
1. Approve and create the public repo `slipstream-live`, and enable Pages from `main` / root.
2. The controller generates the deploy key in WSL with `ssh-keygen -t ed25519 -f ~/.ssh/slipstream_live_deploy -N ""` and shows the user only the **public** key.
3. The user adds it in that repo under Settings → Deploy keys with **write** access.
4. The user runs `touch ~/slipstream-data/publish.enabled`.

Then:
- Run the full CI and the whole-PR reviews, then push and open the PR, and merge when green.
- The first publish happens only after the user completes the steps.
