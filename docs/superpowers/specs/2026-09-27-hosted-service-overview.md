# Hosted Slipstream: Overview and Decomposition

Date: 2026-09-27
Status: Direction approved in conversation. Each sub-project below gets its own spec, plan and PR.

## 1. Goal

Make Slipstream usable by people on the internet without downloading it or using a terminal. It stays **paper-only**: no endpoint can ever place a real order or move money. It must also stay low-latency and secure.

## 2. Decisions (user, 2026-09-27)

| Question | Decision |
|---|---|
| Delivery | **B.** A remote MCP server for AI assistants. **C.** A public API with docs. **E.** A public live leaderboard or research page. The web calculator (A) and the chat bots (D) were not chosen. |
| Hosting | **Oracle Cloud Always Free.** An always-on ARM VM. The user creates the account; its card is used for identity verification only, and the account must never be upgraded. |
| Access | **Open plus a free key tier.** Instant cost estimates need no signup and are rate-limited per IP. Full multi-minute simulations need a free API key, issued through GitHub sign-in. The only personal data kept is the GitHub username. |

## 3. Shape

```
AI assistants (MCP) ─┐
Developers (REST) ───┼─► HTTPS (TLS) ─► API gateway ─► loopback gRPC ─► Slipstream core ─► wss:// Kraken, Coinbase
Browser (static page)┘                  validation, rate limits,        engine + per-venue
                                        size caps, job queue,           feed processes
                                        no real-order path              (one shared connection per venue)
Hourly job ─► paper order through the core ─► results ─► static leaderboard page
```

- **The engine is never exposed.** It keeps binding to loopback only, and the gateway is the only public component.
- **One shared set of exchange connections serves every user.** That keeps latency low and avoids per-user connections that the exchanges could rate-limit.
- **MCP (B) and the leaderboard (E) are thin layers over the API (C).** The core is built once.

## 4. Sub-projects, in order

Each step adds only one new attack surface, and each surface is reviewed before the next step starts.

0. **Historical results database: the proof (added 2026-09-27, user request). Start collecting as early as possible, because evidence only builds up over time.**
   - **What is stored:**
     - Every scheduled paper run: time; order spec; the fees used; per-algorithm fills, all-in cost, fees, slippage and routing gain; the venue split; engine latency stats.
     - Optionally, the raw recorded market data for each run, so any run can be replayed and audited later.
   - **Where it lives:** SQLite on the VM, in WAL mode, append-only. That means one file with no server, which is free, fast and simple.
     - A schema version table guards migrations.
     - Nightly backups go to Oracle Object Storage (Always Free) or a GitHub release asset.
   - **Integrity:**
     - Each row carries the git commit of the code that produced it, so results stay attributable.
     - Rows are never updated; a correction is a new row.
     - Raw recordings get a checksum.
   - **Who uses it:**
     - The leaderboard: charts, confidence bands, the history page.
     - The CSV download.
     - Later, the API's `GET /v1/history` endpoint, and the smarter router, which learns venue reliability and freshness by hour.
   - **Before the VM exists:** the collector can already run on the local machine to start the dataset. It is imported into the VM database once the VM is up.
1. **E: live leaderboard.**
   - An hourly scheduled paper order runs on the Oracle VM. Results are published as a static page generated from the database (item 0).
   - **No inbound surface:** there is no public endpoint, only published files.
   - **The page includes a "Use it yourself" section.**
     - The public API (C) and the MCP server (B) are shown as *coming soon*, with a short description of each.
     - Each section links to its docs once it ships.
2. **C, part 1: instant cost-estimate API.**
   - `POST /v1/estimate` answers in milliseconds with the best fee-adjusted route, the all-in cost and the savings. The engine already computes the "fill everything now" route.
   - Open to anyone, rate-limited per IP, served over HTTPS.
3. **B: remote MCP server.** A thin tool layer over the estimate API, added with one URL in Claude or ChatGPT.
4. **C, part 2: full simulations.**
   - Multi-minute TWAP, VWAP, POV and Almgren-Chriss runs as queued jobs, through free GitHub-sign-in keys.
   - This needs the engine to run many users' orders at once (Phase 3 multi-user work): per-request order isolation, and per-key limits and quotas.

**Prerequisite:** pipeline PR 3 (remove the legacy unary path, add the CI latency benchmark) lands first, so the hosted core runs on the concurrent pipeline.

## 5. Router data expansion (roadmap, separate specs)

These are ordered by value against effort:
1. **Bitstamp and Gemini.** Both are USD pairs with public WebSocket books, so there is no currency conversion, and each costs about the same effort as the Coinbase venue did.
2. **A stablecoin rate feed (USDT/USD, USDC/USD).** This is needed before routing to USDT books.
3. **OKX / Bybit BTC-USDT.** These are among the deepest books, but route through a conversion step and have regional availability limits. That matters only for real trading, never for paper.
4. **Learned venue reliability.** From the historical database (item 0): freshness, spread and depth by hour, used as a routing tie-breaker.
5. **The user's real fee tier.** Optional; it needs an account on each venue, so it is never required.

## 6. Security baseline (applies to every sub-project)

- HTTPS only, with automatic certificates, for example Caddy with Let's Encrypt. A free DNS name if no domain is owned.
- The VM firewall opens only 443, plus 22 for SSH, which is key-only (no passwords) and restricted to the user's IP where practical.
- The gateway enforces strict input schemas, request size caps, per-IP and per-key rate limits, and timeouts.
- No endpoint accepts venue URLs, file paths or engine addresses from users.
- Secrets never go in the repo. They live in environment files on the VM, which only the user edits. CI deploys use a least-privilege token.
- The services run as non-root, in containers, with read-only filesystems where possible.
- Logs never contain secrets or API keys.
