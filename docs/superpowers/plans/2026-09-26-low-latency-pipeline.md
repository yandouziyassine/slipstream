# Low-Latency Concurrent Market Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the per-message unary gRPC calls with one concurrent market stream per venue plus one subscription for fills and order updates.
- The engine core becomes single-threaded, owning all state.
- There are two clock modes: `live`, which cannot be spoofed, and `replay`, which is exact and runs as fast as possible.
- Target: p50 below 0.15 ms per message, where it is about 0.55 ms today, with identical replay results.

**Spec:** `docs/superpowers/specs/2026-09-26-low-latency-pipeline-design.md` (approved 2026-09-26). Read it first; this plan does not repeat its rationale.

**Tech stack:**
- Engine: C++20 with the gRPC 1.51 **sync** server API. Each streaming RPC runs on its own server thread, and that thread is the ingest thread. Tests use GoogleTest.
- Orchestrator: Python 3.12 with `grpc.aio` (grpcio 1.84), `multiprocessing` (spawn), pytest, and `mypy --strict`.

**Rules for every task (CLAUDE.md):**
- **Development:** TDD (red, then green, then refactor).
- **After each task:** run `/security-review` and `/caveman:caveman-review`, and fix valid findings test-first.
- **Commits:**
  - Set the noreply author and committer through the `GIT_AUTHOR_EMAIL` and `GIT_COMMITTER_EMAIL` env vars, never through git config.
  - Every commit carries a `Co-Authored-By` trailer.
  - Run git from Windows, not from WSL.
- **Builds and tests:** run in WSL through scratchpad scripts started with `MSYS_NO_PATHCONV=1 wsl.exe bash /mnt/c/...`. Each script sets `export PATH="$HOME/.venvs/slipstream/bin:$PATH"`.
- **Gate:** `bash scripts/ci.sh` must print `CI OK` before each PR.

**Delivery:** three PRs. Each one leaves main fully working.

---

## PR 1 — Engine (`feat/pipeline-engine`)

After PR 1, old clients still work: the unary `ApplyBookUpdate`, `ApplyTrades` and `Step` calls are served through the engine loop's command queue.

### Task E1: Protocol additions (additive only)
**Files:** `proto/slipstream/v1/execution.proto`.

- [ ] Add the following, exactly as spec §3:
  - the messages `Heartbeat`, `Tick`, `MarketEvent` (a `oneof` of book, trades, heartbeat and tick), `MarketStreamSummary`, `SubscribeRequest`, `OrderUpdate`, and `EngineEvent` (with `seq` and a `oneof` of fill or order);
  - the RPCs `MarketStream` (client streaming) and `Subscribe` (server streaming).
- [ ] Add to `StatusReply`:
  - `repeated VenueBook books = 7`, where `VenueBook` has `venue`, `bids` and `asks`;
  - `EngineStats stats = 8`, with `events`, `queue_high_water`, `latency_p50_ns` and `latency_p99_ns`;
  - `ClockMode clock_mode = 9`, where the enum is `CLOCK_MODE_UNSPECIFIED`, `CLOCK_MODE_LIVE` or `CLOCK_MODE_REPLAY`.
- [ ] Add to `VenueInfo`: `bool has_book = 6` and `bool fresh = 7`.
- [ ] Build, then run `bash scripts/gen_proto.sh`. Every existing C++ and Python test must still pass, because the change is additive.
- [ ] Commit: `feat(proto): add market stream, subscription, and status fields for the concurrent pipeline`.

### Task E2: Engine core additions
**Files:** `engine/src/engine.{h,cpp}`, `engine/tests/engine_routing_test.cpp`, `engine/tests/engine_test.cpp`.

- [ ] **Heartbeat freshness.**
  - Add `bool apply_heartbeat(std::size_t venue, std::int64_t now_ns)`. It records `last_heartbeat_ns` and returns false for an unknown venue.
  - `fresh_locked` uses `effective = max(last_update_ns, min(last_heartbeat_ns, last_update_ns + kHeartbeatGraceNs))`, where `kHeartbeatGraceNs` is 30 s. It then applies the usual `now - effective <= stale_ns` check.
  - Tests:
    - a heartbeat keeps a quiet venue fresh;
    - the venue goes stale when its book has been silent for more than 30 s, even with heartbeats;
    - a heartbeat before any book leaves `has_book` false.
- [ ] **Order updates.**
  - `step()` returns `StepOutput { std::vector<Fill> fills; std::vector<OrderUpdate> updates; }`, where an update is `{order_id, state, reason, filled_qty}`.
  - An update is emitted whenever an order's state changes (Working to Completed or Halted), and also when a fill changes `filled_qty`.
  - Existing callers use `.fills`. The unary `Step` service keeps its reply unchanged.
  - Tests cover each transition and the fill-driven update.
- [ ] **Book and venue introspection.**
  - `std::vector<VenueBookView> books(std::size_t depth) const`: the top-N levels per venue.
  - `bool has_book(v)` and `bool fresh(v, now)`, exposed through `venue_settings()`, or a new `venue_states(now)` if cleaner.
  - Tests for both.
- [ ] Commit per sub-part: `feat(engine): ...`.

### Task E3: Clock and config
**Files:** `engine/src/clock.{h,cpp}` (new), `engine/src/config.{h,cpp}`, `engine/tests/{clock,config}_test.cpp`.

- [ ] **`LiveClock`:** anchors `system_clock` once at construction, then advances with `steady_clock`, so it never goes backwards. `now_ns()` returns int64 nanoseconds since the epoch.
- [ ] **Clock flag:** `--clock live|replay`, defaulting to `live`. `EngineConfig::clock` is an enum. Any other value is a startup error.
- [ ] **Tests:**
  - successive `now_ns()` values never decrease;
  - the value is within 1 s of `system_clock`;
  - config parsing, including a bad value.
- [ ] Commit: `feat(engine): add a monotonic live clock and --clock live|replay`.

### Task E4: `BoundedQueue<T>`
**Files:** `engine/src/bounded_queue.h` (header-only), `engine/tests/bounded_queue_test.cpp`.

- [ ] **Behaviour:** a mutex plus a condition variable around a `std::deque<T>`, with a fixed capacity.
  - `bool try_push(T)`: false when the queue is full or closed.
  - `std::optional<T> pop_for(std::chrono::microseconds)`.
  - `std::optional<T> try_pop()`.
  - `void close()`: wakes every waiter; later pushes fail, and pops keep draining.
  - `size()` and `high_water()`.
- [ ] **Tests:**
  - FIFO order;
  - capacity is enforced;
  - `close()` wakes a blocked pop;
  - 4 producer threads × 25k items with 1 consumer: every item arrives exactly once, and each producer's items stay in order.
- [ ] Commit: `feat(engine): add a bounded MPSC queue`.

### Task E5: `EngineLoop` — single writer, command queue first, publisher, timer, stats
**Files:** `engine/src/engine_loop.{h,cpp}` (new), `engine/tests/engine_loop_test.cpp`.

- [ ] **Types:**
  - `MarketItem { std::size_t venue; std::variant<BookData, TradeData, HeartbeatData, TickData> data; std::int64_t ingest_ns; }`.
  - `BookData` is `{is_snapshot, bids, asks, recv_ns}` and `TradeData` is `{trades}`.
- [ ] **Construction:** `EngineLoop(Engine&, ClockMode, std::size_t market_capacity = 10'000, std::size_t subscriber_capacity = 10'000)` starts the engine thread. In live mode it also starts a timer thread that pushes a `TickData` every 50 ms, skipping the tick when the queue is full.
- [ ] **Market input:** `bool push(MarketItem)`. It returns false when the queue is full; the caller then ends its stream with `RESOURCE_EXHAUSTED`.
- [ ] **Commands:** `template <class F> auto run(F&& fn)` posts `fn(Engine&)` to the **command queue** and blocks on a `std::promise` until the result is ready.
  - The engine thread always drains commands before taking the next market item.
  - The unary Submit, GetStatus, ApplyBookUpdate, ApplyTrades and Step calls all use this path.
- [ ] **Venue binding:**
  - `bool bind_venue(std::size_t)` returns false when that venue is already bound.
  - `void unbind_venue(std::size_t)`.
  - In replay mode, `bind_replay()` allows only one replay stream.
- [ ] **Subscription:**
  - `std::shared_ptr<Subscriber> subscribe()`. It returns nullptr when a subscriber already exists (one at a time).
  - `Subscriber` has `std::optional<EngineEventData> pop_for(timeout)`, plus an `overflowed()` flag that is set when its bounded queue is full. Once overflowed it stops receiving events.
  - `unsubscribe()` happens when the stream ends.
- [ ] **Engine thread loop:**
  1. Drain the commands.
  2. Pop a market item with a timeout of 1 ms.
  3. Compute the event time:
     - live: the engine clock at ingest (`ingest_ns`);
     - replay: the item's `recv_ns`, or `TickData.now_ns`.
  4. Apply the item: book, trades, heartbeat, or a tick, which only advances time.
  5. Run `engine.step(t)`.
  6. Publish each fill and update as an `EngineEventData` with `seq` counting up from 1.
  7. Record `now_steady - ingest_steady` in a 32-bucket log2 histogram, and update `events` and `queue_high_water`.
- [ ] **Stats:** `stats()` runs as a command. It computes p50 and p99 from the histogram, reporting the upper bound of the matching bucket.
- [ ] **Shutdown:** `~EngineLoop()` and `stop()` close the queues, let the engine thread drain everything left, join both threads, and close the subscriber.
- [ ] **Tests** (Engine with zero venue rules, two venues):
  - market items are applied in order;
  - a command queued behind 1,000 market items runs before them;
  - `bind_venue` twice returns false;
  - a second `subscribe` returns nullptr;
  - a full subscriber sets `overflowed`;
  - a live-mode timer tick triggers a TWAP slice with no market data;
  - replay mode has no timer (no fills without events);
  - `seq` is strictly increasing;
  - `stop()` drains every queued item.
- [ ] Commit: `feat(engine): add a single-writer engine loop with command queue, subscription and stats`.

### Task E6: Service — MarketStream, Subscribe, legacy calls through the loop, main wiring
**Files:** `engine/src/service.{h,cpp}`, `engine/src/main.cpp`, `engine/tests/service_test.cpp`, and a new `engine/tests/stream_test.cpp`.

- [ ] **Validation refactor.** Move today's BookUpdate and TradeBatch checks into functions returning `std::variant<MarketItem, grpc::Status>`:
  - symbol;
  - venue allowlist;
  - level caps;
  - `recv_ns >= 0`;
  - finite, positive levels (the engine re-checks these).
- [ ] **`MarketStream(ctx, reader, summary)`:**
  - **Live mode:**
    - Read the `slipstream-venue` metadata. A missing or unknown venue gives `INVALID_ARGUMENT`, and a venue that is already bound gives `FAILED_PRECONDITION`. The venue is unbound in a scope guard.
    - An event's own venue must be empty or equal to the bound venue.
    - A non-zero `recv_ns`, or any `Tick`, gives `INVALID_ARGUMENT`.
  - **Replay mode:**
    - A second concurrent replay stream gives `FAILED_PRECONDITION`.
    - Each book or trade event must name a venue, and every book event or tick needs a time.
    - Times must never go backwards and must not jump more than 86,400 s past the previous event. Either failure gives `INVALID_ARGUMENT`.
  - For each event: validate, then `loop.push`. A failed push gives `RESOURCE_EXHAUSTED`.
  - Return `summary.events`.
- [ ] **`Subscribe(ctx, req, writer)`:**
  - `loop.subscribe()` returning nullptr gives `FAILED_PRECONDITION`.
  - Loop on `pop_for(50ms)`; on each event, `writer->Write`.
  - Stop and return OK when `ctx->IsCancelled()`.
  - When `overflowed()` is set, return `RESOURCE_EXHAUSTED`.
- [ ] **Legacy unary calls:**
  - `ApplyBookUpdate`, `ApplyTrades`, `Step`, `SubmitParentOrder` and `GetStatus` run through `loop.run(...)`.
  - In live mode, a client `recv_ns`, `now_ns` or `start_ns` is **ignored** and replaced by engine time. It is not rejected, so current Python clients keep working until PR 2.
  - `GetStatus` fills in `books` (depth equals the engine book depth), `has_book`, `fresh`, `stats` and `clock_mode`.
- [ ] **`main.cpp`:**
  - Build `LiveClock`, `Engine`, `EngineLoop(engine, config.clock)` and the service.
  - Print the clock mode on the startup line. Replay mode prints `REPLAY CLOCK` in capitals.
  - On a signal: `server->Shutdown()`, then `loop.stop()`.
- [ ] **Tests** (`stream_test.cpp`): a real in-process server using `grpc::ServerBuilder` with `BuildAndStart()` and `server->InProcessChannel({})`:
  - two venues stream at the same time and both books apply;
  - a duplicate venue is rejected;
  - live mode rejects a non-zero `recv_ns` and a `Tick`;
  - replay mode rejects time that goes backwards, a jump over one day, and a missing venue;
  - Subscribe receives fills and a terminal update after a Submit;
  - a second Subscribe is rejected;
  - an invalid event ends only its own stream.
- [ ] **`python/tests/conftest.py`:** engines start with `--clock replay`. Tests replay recorded time, so existing integration tests keep their exact numbers.
- [ ] **Gate:** full `bash scripts/ci.sh` prints `CI OK`, with **unchanged Python expectations**.
- [ ] Commit per sub-part: `feat(engine): ...`, `test(engine): ...`.

### Task E7: ThreadSanitizer build and stress test
**Files:** `engine/CMakeLists.txt`, `scripts/ci.sh`, `engine/tests/stream_stress_test.cpp`.

- [ ] **Build option:** `option(SLIPSTREAM_TSAN ...)` adds `-fsanitize=thread`. It cannot be combined with ASan; `cmake` fails if both are ON.
- [ ] **Stress test:**
  - Two in-process streams send 50k book deltas each, generated deterministically per venue, at the same time.
  - The final `books()` must equal the result of applying the same per-venue sequences one after another to a fresh Engine.
- [ ] **`ci.sh`:** add a step `cmake -S engine -B build/tsan -DSLIPSTREAM_TSAN=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo && cmake --build build/tsan && ctest --test-dir build/tsan -R "Loop|Stream|BoundedQueue" --output-on-failure`.
- [ ] Commit: `test(engine): run the pipeline under ThreadSanitizer with a concurrent stress test`.

**PR 1 finish:**
- Whole-branch `/security-review` and `/caveman:caveman-review`.
- `CI OK`.
- Push, open the PR, and merge when green (standing approval).

---

## PR 2 — Python (`feat/pipeline-python`)

### Task P1: Async engine client
**Files:** `python/slipstream/engine_stream.py` (new), `python/tests/test_engine_stream.py`.

- [ ] **`class EngineChannel`:** one `grpc.aio` channel to the loopback-validated address.
  - `async submit(spec, start_ns, params)`.
  - `async status() -> pb.StatusReply`.
  - `async venue_fees()` and `async book_depth()`, ported from `EngineClient`.
  - `async wait_ready(timeout)`.
- [ ] **`class MarketStreamWriter`:**
  - `open(channel, venue: Venue | None)`: a venue for live mode (sent as metadata), or None for replay.
  - `send_nowait(event: pb.MarketEvent)`: puts the event on a bounded `asyncio.Queue(1000)`. A full queue raises `EngineBackpressureError`.
  - A background task writes from the queue to the stream.
  - `async close()`: finishes the stream and returns the summary. An RPC error is raised as `EngineError` with its code.
- [ ] **`class Subscription`:** an async iterator over `EngineEvent`. It checks that `seq` strictly increases and raises `EngineError` otherwise.
- [ ] **Converters:** `book_event(update, recv_ns | None)`, `trade_event(batch)`, `heartbeat_event()` and `tick_event(now_ns)`.
- [ ] **Tests:**
  - against the real engine binary, using the conftest fixtures in both clock modes:
    - a book and trade round trip;
    - a live-mode rejection surfaces as `EngineError(INVALID_ARGUMENT)`;
    - Subscribe yields fills after a submit;
  - a backpressure unit test with a fake stream.
- [ ] Commit: `feat(orchestrator): add an asyncio gRPC client for market streams and subscriptions`.

### Task P2: Per-venue feed process
**Files:** `python/slipstream/feed_process.py` (new), `python/tests/test_feed_process.py`.

- [ ] **Entry point:** `run_feed(venue, symbol, depth, engine_address, url, error_queue)` is the `spawn` target. It runs one asyncio loop:
  1. Connect the WebSocket.
  2. Subscribe.
  3. For each message:
     - parse it with the venue parser;
     - send book and trade updates as MarketEvents;
     - send heartbeats (a parser returning None for a heartbeat or status message) as `Heartbeat` events.
  4. On any exception: put `(venue, type name, message)` on `error_queue`, then exit with code 1.
  5. On SIGTERM: close the stream and exit with code 0.
- [ ] **`class FeedSupervisor`:** starts one process per venue.
  - `async wait_first_error()` watches the error queue and each process's exit code.
  - `stop()` terminates the processes, then joins them with a 5 s timeout, then kills them.
- [ ] **Tests:**
  - with local `ws://` servers (the existing pattern) and the real engine in live mode: two feed processes stream at the same time, and the engine status shows both `has_book` and `fresh`;
  - a feed hitting a parser error is reported with its venue and message;
  - `stop()` leaves no orphan processes.
- [ ] Commit: `feat(orchestrator): run each venue feed in its own process`.

### Task P3: Session orchestration (replaces the runner's hot path)
**Files:** `python/slipstream/session.py` (new), `python/slipstream/cli.py`, `python/slipstream/replay.py`, `python/slipstream/live.py`, tests.

- [ ] **Clock-mode guard.** At startup, check `StatusReply.clock_mode`:
  - `live` and `compare` without `--file` require `LIVE`;
  - `replay` and `compare --file` require `REPLAY`;
  - on a mismatch, log a clear error and exit 1.
- [ ] **`async run_live_session(...)`:**
  1. Start the `FeedSupervisor`.
  2. Poll `status()` every 50 ms until every venue has `has_book` and `fresh`. Give up after 30 s with `LiveFeedError`.
  3. Build the consolidated fee-adjusted book from `status.books`, reusing `book.consolidated_book` with `LocalBook` built from the reply, and calibrate.
  4. Submit every order.
  5. Consume `Subscription` until every order has a terminal `OrderUpdate`, or until the deadline or the first feed error.
  6. Stop the feeds.
  7. Return statuses from `status()`.
- [ ] **`async run_replay_session(...)`:**
  1. Open a single replay `MarketStreamWriter` (venue None).
  2. Read the replay file in order, converting each record to a MarketEvent with its `recv_ns` (skipping venues that are not configured, as today). Submit once every venue's first snapshot has been sent, calibrating from the books seen so far.
  3. Consume the subscription concurrently.
  4. Close the stream.
  5. Wait for terminal updates. If the file ends with orders still working, send a `Tick` at the last `recv_ns` plus each order's duration so their deadlines resolve deterministically.
- [ ] **`cli.main` switches to the sessions.** Keep the existing error handling, the best-effort summary and the output formats unchanged.
- [ ] **Tests:** every existing integration test (`test_integration_replay`, `_schedules`, `_routing`) must pass **unchanged**, now through `run_replay_session`. If a number differs, investigate. Never loosen an expectation to make a test pass.
- [ ] Commit per sub-part.

### Task P4: Demo scripts and live check
- [ ] The demo scripts start the engine with `--clock live`, which is the default, so they need no flag change. Confirm this.
- [ ] Run `scratchpad/show.sh 60` and check that:
  - every order ends in a terminal state with a reason;
  - fills are at or above each venue's minimum;
  - there is no `-0.00`;
  - latency stats appear in the startup or summary log line (add a `stats` log line at the end of the run).
- [ ] The user checks the output in their terminal before merging.

**PR 2 finish:** reviews, `CI OK`, push, PR, then merge when green.

---

## PR 3 — Cleanup and benchmark (`chore/pipeline-cleanup`)

### Task C1: Remove the legacy unary market path
- [ ] **Proto:** remove the `ApplyBookUpdate`, `ApplyTrades` and `Step` RPCs, plus `BookAck`, `TradeAck`, `StepRequest` and `StepReply`. Reserve their field numbers and names.
- [ ] **Code:** delete the matching service handlers, `EngineClient` (the sync client), `runner.ExecutionRunner`'s hot path, and `live.run_live`'s single-process path. Port or delete their tests.
- [ ] **Gate:** `CI OK`.

### Task C2: Benchmark in CI
- [ ] `bench/pipeline_bench.py` runs a synthetic deterministic stream of 50k events: book deltas on two venues, plus trades. It uses the release engine in replay mode with a POV order that fills often.
  - Latency is measured client-side, from `send_nowait` to receipt of the matching fill. Fills are matched by order and step.
  - It also reads `EngineStats`.
  - It prints p50, p99, p99.9 and events per second.
- [ ] `bench/baseline.json` holds the first measured values. `scripts/bench_pipeline.sh` fails if p50 is more than 2× the baseline.
- [ ] Add it to `scripts/ci.sh` after the tests. It builds the release engine as part of the step.

### Task C3: Docs
- [ ] README:
  - update the architecture diagram;
  - add a "Performance" section with the measured table: before, which was 1.28 ms debug and 0.86 ms release, then about 0.55 ms after Phase 1; and after, the benchmark's p50, p99 and msgs/s;
  - explain the clock modes.
- [ ] `note.md`: decisions and the dev log.

**PR 3 finish:** reviews, `CI OK`, push, PR, then merge when green.

---

## Self-review against the spec

Each spec section maps to these tasks:

| Spec section | Tasks |
|---|---|
| §1 success criteria | E7 (TSan), P3 (identical replay numbers), C2 (latency target) |
| §2 decisions | E3 (clock), E5 (single writer, command queue first), E6 (per-venue streams), P2 (process per venue), E2 (heartbeat freshness moved into the engine) |
| §3 protocol | E1, E6 |
| §4 engine | E4, E5, E6; shutdown in E5 and E6 |
| §5 Python | P1, P2, P3; `record` is unchanged |
| §6 security | E6 (validation, binding, clock rejection, bounded queues, replay mode logged); loopback is unchanged |
| §7 testing | E4–E7, P1–P3, C2 |
| §8 delivery | the three PRs above |
