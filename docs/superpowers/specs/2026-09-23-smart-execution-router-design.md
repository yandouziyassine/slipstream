# Smart Execution Router — Design

Date: 2026-09-23
Status: Approved (v1 design)

## 1. Purpose

Build a smart order-execution engine that minimizes market impact/slippage when
placing orders, similar to infrastructure institutional trading desks use. v1
targets crypto (single exchange, paper trading). Real-world goal: demonstrate a
working, secure, reliable execution system — not just a tech demo — while
building a strong C++/Python/finance portfolio piece.

## 2. Architecture

Two-service split, communicating over local gRPC:

```
┌─────────────────────────────┐         gRPC          ┌──────────────────────────────┐
│   Python Orchestrator       │◄──────────────────────►│   C++ Execution Engine        │
│                              │                        │                                │
│ - Exchange API adapters      │                        │ - Order book reconstruction   │
│   (crypto REST/WS, paper)    │                        │ - TWAP/VWAP execution algos   │
│ - Backtest runner            │                        │ - Order routing logic         │
│ - CLI + structured logging   │                        │ - Risk/position limit checks  │
│ - Config loader (.env/yaml)  │                        │ - Slippage/fill simulation    │
│ - Secrets handling           │                        │   (paper mode)                │
└─────────────────────────────┘                        └──────────────────────────────┘
```

- **Python Orchestrator**: talks to the exchange, normalizes market data, forwards to
  the C++ engine via gRPC, receives execution decisions, logs everything, runs
  backtests offline against historical data.
- **C++ Engine**: owns the order book, runs TWAP/VWAP execution algos, enforces hard
  risk limits before any "would execute" decision is returned. In paper mode,
  simulates fills against real order-book data instead of sending to an exchange.
- **Boundary contract**: a `.proto` file is the single source of truth for what
  crosses the boundary. Python never reaches into C++ internals; C++ never calls
  exchange APIs directly. Each side is independently testable/mockable.

Rationale for this split over a monolith (pybind11) or Python-first prototype:
matches the 1-week-MVP / 1-month-full timeline (thin vertical slice week 1, engine
hardens over the month), gives clean units for parallel-agent development via git
worktrees, and is the stronger systems-engineering story for finance/quant-dev
roles (defined RPC contract, C++ perf-critical component, Python glue).

## 3. Data Flow

```
Exchange WS/REST (crypto, free tier)
        │  raw ticks/order-book updates
        ▼
Python: MarketDataAdapter
        │  normalize → common schema (protobuf)
        ▼
gRPC stream → C++ OrderBookEngine
        │  reconstruct book, compute features (spread, imbalance, vol)
        ▼
C++ ExecutionAlgo (TWAP/VWAP)
        │  decide: slice size, timing, venue (single venue v1)
        ▼
C++ RiskCheck (hard limits: max notional, max position, rate limit)
        │  pass/reject
        ▼
   ── paper mode (v1 default & only mode) ──     ── live mode (gated, post-v1) ──
   C++ FillSimulator                               gRPC → Python BrokerAdapter → exchange
   (matches against real book)
        │
        ▼
Python: Logger/PnL tracker → structured JSON logs → local file / SQLite
        │
        ▼
CLI (tail logs, show open orders, PnL, slippage vs. benchmark)
```

Risk checks happen in C++ *before* the paper/live fork, so live mode (when enabled)
inherits the same safety gate — nothing new to bypass.

Backtests reuse the exact same C++ engine offline: historical data file feeds the
OrderBookEngine/ExecutionAlgo/RiskCheck path instead of a live socket, so backtest
logic never drifts out of sync with live behavior.

## 4. Security Model

- **Secrets**: never committed. `.env` (gitignored) holds API keys, loaded via
  `python-dotenv`. `input.md` (gitignored) is the channel through which the user
  supplies credentials/links directly — Claude does not source or store secrets
  itself.
- **Live-mode gate**: `PAPER_MODE=true` by default. Switching to live requires the
  user to manually edit config and confirm — never flipped automatically.
- **Input validation**: all external data (exchange responses, gRPC messages) is
  bounds-checked/validated before use.
- **gRPC channel**: localhost-only for v1 (no auth needed, same machine). TLS +
  token auth if ever split across hosts.
- **Dependency hygiene**: pinned versions (`requirements.txt`, CMake/vcpkg lock),
  `pip-audit` in CI.
- **Static analysis**: C++ built with `-Wall -Wextra -fsanitize=address,undefined`
  in debug/test builds; Python linted with `ruff`, type-checked with `mypy`.
- **`/security-review`**: required before merging any PR touching execution,
  risk-check, or secrets-handling code.
- **Logging**: secrets/API keys never logged, redacted by default.
- **Git hygiene**: `.gitignore` covers `.env`, `input.md`, build artifacts, local
  DB files.

## 5. Testing Strategy

- **Unit**: C++ (GoogleTest/Catch2) for order-book logic, TWAP/VWAP math, risk
  checks. Python (pytest) for adapters, config, logging.
- **Integration**: full gRPC round-trip between the two services, using
  recorded/replayed market-data fixtures (no live network in CI).
- **Property-based**: order-book invariants (best bid ≤ best ask, no negative
  sizes, position never exceeds risk limit under randomized sequences) via
  `rapidcheck` (C++) / `hypothesis` (Python).
- **Fuzz testing**: malformed/adversarial exchange payloads fed to the
  market-data parser (libFuzzer), catching crashes/UB on untrusted input.
- **Backtest-as-regression-test**: a known historical scenario (e.g. a documented
  flash-crash window) run through the full pipeline, asserting risk checks
  correctly throttle/reject.
- **CI**: GitHub Actions — build (with sanitizers) + full test suite + lint +
  dependency audit on every PR.
- **Workflow**: TDD (`/superpowers:test-driven-development`) governs feature work.

## 6. Repo / Dev Workflow

- **`CLAUDE.md`**: read before every action. Coding standards + required skill
  triggers:
  - `/superpowers:using-superpowers` — every session start
  - `/superpowers:brainstorming` — before new features/design changes
  - `/superpowers:test-driven-development` — governs all implementation
  - `/superpowers:using-git-worktrees` — parallel/isolated feature branches
  - `/superpowers:dispatching-parallel-agents` — independent C++/Python work
  - `/superpowers:subagent-driven-development` — delegating bounded subtasks
  - `/superpowers:receiving-code-review` — after review findings
  - `/superpowers:verification-before-completion` — before marking work done
  - `/caveman:caveman`, `/caveman:cavecrew` — comms/dev style
  - `/security-review` — mandatory before merging execution/risk/secrets code
- **`note.md`**: living doc — project explanation, architecture, decision
  rationale, dev log.
- **`input.md`**: gitignored running ask-list — what's needed, why, where to get
  it (exchange API keys, etc.).
- **Branching**: git worktrees for parallel feature branches, PRs into `main`, no
  direct pushes to `main`.
- **Commits**: incremental, honest dev history (no squash-dumping), each carrying
  `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`; repo/README notes
  Claude Code was used in development.
- **GitHub**: public repo, created and pushed only after explicit user approval
  each time — no autonomous publishing.

## 7. Roadmap

**Week 1 (MVP, pushed to public GitHub):**
- Repo scaffold: CMake (C++), Poetry/venv (Python), `.proto` contract, CI skeleton
- `CLAUDE.md`, `note.md`, `input.md`
- Single crypto exchange adapter, paper mode only
- C++ order-book reconstruction + TWAP execution + hard risk-limit check
- gRPC wired end-to-end, paper fills simulated, CLI shows orders/PnL
- Core unit tests passing, CI green

**Weeks 2-4:**
- Add VWAP + Almgren-Chriss-style execution algo option
- Second venue (broker) adapter — real "smart routing" (cheaper/faster venue pick)
- Property-based + fuzz tests, `/security-review` pass on execution path
- Backtest runner + flash-crash regression scenario
- Hardened risk checks (rate limiting, circuit breaker on abnormal fills)
- Docker container (multi-stage build, both services)
- `note.md` finalized as full writeup

**Stretch (post-month, requires explicit user confirmation):**
- Live-mode wiring, gated and manually enabled by the user only.

## 8. Explicit Non-Goals (v1)

- No live trading (paper/simulation only until user explicitly confirms otherwise
  post-month-1).
- No multi-venue routing in week 1 (single exchange first).
- No RL-based execution in v1 (deterministic TWAP/VWAP first — provable,
  reliable, matches "simple secure code that works" principle).
- No cloud deployment in week 1 (Docker container is an end-of-project item).
