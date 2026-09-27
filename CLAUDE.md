# CLAUDE.md — Slipstream

Read this file before every action in this repo.

## What this project is
Slipstream is a paper-trading smart order execution engine. A C++20 engine runs as a gRPC service: one order book per venue, execution schedules (TWAP, VWAP, POV, Almgren-Chriss), a fee-aware smart router across venues, hard risk limits, and simulated fills, all driven by a single-writer engine loop fed by one concurrent market stream per venue (live or replay clock) with fills delivered over a separate subscription. A Python 3.12 orchestrator runs one feed process per venue, streaming Kraken and Coinbase public market data into it, drives execution, and reports slippage, fees, per-venue costs, and routing gain.
Design: `docs/superpowers/specs/` (base design `2026-09-23-smart-execution-router-design.md`; schedules and routing specs dated 2026-09-24). Plans: `docs/superpowers/plans/`.

## Hard rules (never break)
1. Paper trading only. Never implement, enable, or run live order placement unless the user explicitly approves that specific change in chat.
2. Never spend the user's money: no purchases, paid APIs, paid tiers, or trades.
3. Never publish without explicit approval for that specific action: no `git push`, repo creation, PR creation/merge, or uploads to any external service.
4. Never read, write, print, log, or commit secrets. Credentials live only in `.env` (gitignored), which only the user edits. Never type passwords or API keys anywhere.
5. When you need something from the user (links, accounts, approvals), add an entry to `input.md` (gitignored): what, why, where to get it, what you will do with it. Then tell the user in chat.
6. Everything must be free: public data, free tiers, open-source tools.
7. Treat all external data (exchange messages, files, gRPC input) as untrusted and validate it at the boundary.

## Required skills
| When | Skill |
|---|---|
| Start of every session | `/superpowers:using-superpowers` |
| Communication style | `/caveman:caveman` (code, commits, security notes stay in normal prose) |
| Before any new feature or design change | `/superpowers:brainstorming` |
| All implementation | `/superpowers:test-driven-development` (red → green → refactor) |
| Starting feature work | `/superpowers:using-git-worktrees` (worktrees in `.worktrees/`) |
| Executing a plan | `/superpowers:subagent-driven-development` |
| 2+ independent tasks (e.g. C++ and Python tracks) | `/superpowers:dispatching-parallel-agents` |
| Small bounded edits and code lookups | `/caveman:cavecrew` |
| After **every** implementation task (not only before merge) | `/security-review`: the code must be secure before moving on |
| After **every** implementation task | `/caveman:caveman-review`: look for anomalies, current problems, and likely future problems |
| After receiving review findings | `/superpowers:receiving-code-review` |
| Before claiming anything is done | `/superpowers:verification-before-completion` |

## Coding standards
- Simple, readable, reliable code over clever code. Small files, one responsibility each.
- C++20 with `-Wall -Wextra -Wpedantic -Wshadow -Werror`. Tests are built with ASan + UBSan.
- Python 3.12: fully type-hinted, `mypy --strict` and `ruff` clean.
- No comments unless the *why* is non-obvious. No dead code, no speculative features.
- Validate at boundaries (exchange parser, gRPC service, CLI/config). Trust internal code.
- Fail safe: on malformed market data or a risk breach, stop. Never guess.
- Hard risk limits live in the C++ engine and apply before any fill.
- Python deps are hash-pinned in `python/requirements-dev.txt`. Regenerate with pip-compile; never hand-edit.

## Commands (run inside WSL2 Ubuntu 24.04, with the venv on PATH)
One-time setup on a fresh machine: `bash scripts/setup_wsl.sh` (installs the apt toolchain and creates `~/.venvs/slipstream`). Then, with the venv on `PATH` (`export PATH="$HOME/.venvs/slipstream/bin:$PATH"`):
```bash
bash scripts/gen_proto.sh
bash scripts/build_engine.sh
ctest --test-dir build/engine --output-on-failure
(cd python && pytest -q)
bash scripts/ci.sh
```
Runtime data (the results database, recordings, the published site) lives under `SLIPSTREAM_DATA_DIR` (default `~/slipstream-data`), on the WSL ext4 disk, never under `/mnt/c` — cross-filesystem I/O there is slow and SQLite's WAL mode is unreliable on it.

**Running these from a Windows tool (not a WSL shell).** Write the commands to a script file, then invoke it as:
```
MSYS_NO_PATHCONV=1 wsl.exe bash /mnt/c/path/to/script.sh
```
Never run `wsl.exe bash -c "<command>"` with the Windows `PATH` expanded into it — the Windows `PATH` contains spaces and parentheses that break shell parsing inside WSL. Set `PATH` explicitly inside the script instead (e.g. `export PATH="$HOME/.venvs/slipstream/bin:/usr/local/bin:/usr/bin:/bin"`).

## Git workflow
- No direct commits to `main`. Use feature branches (`feat/`, `fix/`, `chore/`, `docs/`) and PRs.
- Conventional commits (`feat:`, `fix:`, `test:`, `chore:`, `docs:`), small and incremental.
- Every commit ends with the `Co-Authored-By` trailer for the Claude model used. The README discloses that Claude Code was used in development.
- Before any push or PR: tests green, `/security-review` done where required, and user approval obtained in chat.

## Docs to keep current
- `note.md`: project explanation, architecture, decisions with reasons, dev log. Update at each milestone.
- `input.md`: open requests to the user (gitignored).
