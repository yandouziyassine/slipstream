# CLAUDE.md — Slipstream

Read this file before every action in this repo.

## What this project is
Slipstream is a paper-trading smart order execution engine. A C++20 engine runs as a gRPC service: one order book per venue, execution schedules (TWAP, VWAP, POV, Almgren-Chriss), a fee-aware smart router across venues, hard risk limits, and simulated fills. A Python 3.12 orchestrator streams Kraken and Coinbase public market data into it, drives execution, and reports slippage, fees, per-venue costs, and routing gain.
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

## Commands (run inside the dev container; Docker Desktop must be running)
```bash
docker compose build dev
docker compose run --rm dev bash scripts/gen_proto.sh
docker compose run --rm dev bash scripts/build_engine.sh
docker compose run --rm dev ctest --test-dir build/engine --output-on-failure
docker compose run --rm dev bash -c "cd python && pytest -q"
docker compose run --rm dev bash scripts/ci.sh
```

## Git workflow
- No direct commits to `main`. Use feature branches (`feat/`, `fix/`, `chore/`, `docs/`) and PRs.
- Conventional commits (`feat:`, `fix:`, `test:`, `chore:`, `docs:`), small and incremental.
- Every commit ends with the `Co-Authored-By` trailer for the Claude model used. The README discloses that Claude Code was used in development.
- Before any push or PR: tests green, `/security-review` done where required, and user approval obtained in chat.

## Docs to keep current
- `note.md`: project explanation, architecture, decisions with reasons, dev log. Update at each milestone.
- `input.md`: open requests to the user (gitignored).
