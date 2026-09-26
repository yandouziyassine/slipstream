# Low-Latency Concurrent Market Pipeline — Design

Date: 2026-09-26
Status: Approved in conversation; written spec awaiting user review
Scope: Phase 2, sub-project 1 of 5. The later sub-projects are:
- 2: feed reconnect and liveness
- 3: order API (fill sequence numbers, idempotent submit, cancel)
- 4: paper fills that consume liquidity
- 5: Kraken checksum

This spec is also the foundation for the Phase 3 multi-user architecture.

## 1. Purpose and success criteria

Today every market message costs up to three blocking unary gRPC calls, and each round trip over loopback takes about 250–300 µs. Phase 1 removed the per-message status call. Measured with the release engine, a message still costs about 0.55 ms. The engine's own work (apply, route, risk) is only a few microseconds.

Two things are wrong with the current design:
- **The transport is the bottleneck.**
- **Every venue shares one Python event loop.** A 5 MB Coinbase snapshot being parsed delays Kraken prices.

Goals:
- **Fast.** Latency from market message to engine is p50 < 0.15 ms and p99 < 1 ms, measured in CI, from the moment Python sends an event until its fill (if any) is emitted.
- **Concurrent.** Each venue is received, parsed and sent in parallel, so one venue's latency never depends on another's.
- **Correct.** Every existing replay fixture produces **identical** fills and costs through the new pipeline, and the ThreadSanitizer build is clean.
- **Safe.** Validation stays at the boundary, and clients cannot spoof the clock in live mode. Every queue is bounded, and the pipeline fails safe instead of silently dropping data.

## 2. Decisions

| Question | Decision | Why |
|---|---|---|
| Transport | A long-lived gRPC stream per venue, replacing unary calls | Removes per-message round trips (about 5–10× less overhead) and keeps the engine a separate, isolated service. That is the shape multi-user needs. Rejected: embedding the engine in Python, which is fastest but blocks multi-user and a crash takes Python down with it; and market data in C++, which is the most work and moves untrusted parsing into C++. |
| Concurrency | One Python process and one stream per venue. The engine gives each stream its own ingest thread, and all streams feed **one** engine core | Python runs one thread at a time per process, so only separate processes parse in parallel. The engine core stays single-threaded so the router always compares every venue in one consistent snapshot. Its work takes microseconds, so it is not the bottleneck. |
| Clock | `--clock live` (the default): the engine stamps events with its own clock and ignores client time. `--clock replay`: recorded times, which must never go backwards, with no timer | Live time cannot be spoofed, and replays stay exact and run as fast as the CPU allows |
| Fills and order updates | One separate server-streaming **subscription** | A fill can be triggered by any venue's update, so it cannot belong to one venue's stream. *(Refinement of the approved "bidirectional stream": market data flows in on per-venue streams, and engine events flow out on one subscription.)* |
| Order entry | `SubmitParentOrder` and `GetStatus` stay unary | They are rare, and they must never wait behind market data |
| Freshness | Moves into the engine. A heartbeat refreshes a venue only up to 30 s after its last real book change | The engine is the single source of truth; this is the same rule as Phase 1, applied in one place |

## 3. Protocol (`proto/slipstream/v1/execution.proto`)

```proto
service ExecutionEngine {
  // One per venue in live mode; the venue comes from the "slipstream-venue" request metadata.
  // Replay mode: exactly one stream, and every book or trade event names its own venue.
  rpc MarketStream(stream MarketEvent) returns (MarketStreamSummary);
  // Fills and order state changes for every order, in the engine's order. One subscriber at a time.
  rpc Subscribe(SubscribeRequest) returns (stream EngineEvent);
  rpc SubmitParentOrder(ParentOrder) returns (SubmitReply);   // unchanged; start_ns ignored in live mode
  rpc GetStatus(StatusRequest) returns (StatusReply);         // + venue freshness, books, engine stats
  // Temporary, removed in PR 3: ApplyBookUpdate, ApplyTrades, Step.
}

message Heartbeat {}
message Tick { int64 now_ns = 1; }                 // replay mode only: advances time
message MarketEvent {
  oneof event { BookUpdate book = 1; TradeBatch trades = 2; Heartbeat heartbeat = 3; Tick tick = 4; }
}
message MarketStreamSummary { uint64 events = 1; }

message SubscribeRequest {}
message OrderUpdate {
  string order_id = 1;
  OrderState state = 2;
  string reason = 3;
  double filled_qty = 4;
}
message EngineEvent {
  uint64 seq = 1;                                  // strictly increasing across all events
  oneof event { Fill fill = 2; OrderUpdate order = 3; }
}
```

`StatusReply` gains the following fields:
- `repeated VenueBook books`: each venue's top-N levels, used for calibration before submit.
- `VenueInfo.has_book` and `VenueInfo.fresh`.
- `EngineStats stats`:
  - events processed;
  - queue high-water mark;
  - p50 and p99 of ingest-to-processed time, from a fixed-bucket histogram.

In live mode, a non-zero `BookUpdate.recv_ns` and any `Tick` are rejected (`INVALID_ARGUMENT`), because they would let a client supply time.

## 4. Engine (C++)

```
gRPC stream threads (one per MarketStream)        engine thread (owns ALL state)
  metadata venue → allowlist + not already bound  ┌─ pop event (bounded MPSC queue, 10,000)
  decode + validate event (same rules as today)   │  stamp time (live: engine clock at ingest)
  push {venue, event, ingest_ns} ─────────────────►│  apply book / trades / heartbeat
                                                  │  step all orders (after every event;
  timer thread (live mode): push Tick every 50 ms ►│    live mode also on each 50 ms Tick)
                                                  │  publish Fill / OrderUpdate with seq ──► subscriber queue (bounded)
                                                  └─ unary Submit / GetStatus: separate command queue, drained first
```

- **Single writer.** Only the engine thread touches books, orders and risk state.
  - Unary `SubmitParentOrder` and `GetStatus` go into a **separate command queue**. The engine thread drains it before taking the next market event, so order entry never waits behind a burst of market data. The gRPC thread waits on a `std::promise` for the result, and ordering stays consistent because only the engine thread applies either kind.
  - The current global mutex leaves the hot path.
- **Venue binding (live mode).**
  - The `slipstream-venue` metadata must name a registered venue. The event's own venue field, if present, must be empty or match.
  - Only one open stream per venue is allowed; a second one gets `FAILED_PRECONDITION`.
  - When a stream ends, its venue is unbound, and it goes stale under the normal freshness rule.
- **Replay mode.**
  - Exactly one market stream. Every event carries a venue (for book and trade events) and a time.
  - Time must never go backwards and must not jump more than 1 day past the previous event. Otherwise the result is `INVALID_ARGUMENT`.
  - There is no timer; time advances only through events and `Tick`.
  - Startup logs `REPLAY CLOCK` prominently.
- **Backpressure.**
  - A full engine queue ends the offending stream with `RESOURCE_EXHAUSTED`. Data is never silently dropped.
  - A subscriber that falls more than 10,000 events behind is disconnected with `RESOURCE_EXHAUSTED`. Order state stays available through `GetStatus`.
- **Invalid input.** An invalid event ends that stream with `INVALID_ARGUMENT`, and none of its data is applied.
- **Shutdown.** On SIGINT/SIGTERM the engine stops accepting new streams, drains the queue in order, finishes publishing, then exits.
- **Stats.**
  - Counters are kept on the engine thread and snapshotted through the `GetStatus` command.
  - The ingest-to-processed histogram has fixed log2 buckets (no allocation), from 1 µs to 1 s.

## 5. Python orchestrator

- **One feed process per venue.**
  - Each is started with `multiprocessing` using the `spawn` method.
  - It runs its own asyncio loop: WebSocket, then parse (the existing parsers), then send `MarketEvent` on a `grpc.aio` `MarketStream`, with no waiting per message.
  - Heartbeats become `Heartbeat` events.
  - Each feed has a bounded send queue (1,000 events). A full queue means the engine is not keeping up, so the feed stops with an error rather than buffering without limit.
- **Main process.**
  - **Waiting for books:** it polls `GetStatus` every 50 ms until every configured venue `has_book` and is `fresh`. It gives up after 30 s with a clear error.
  - **Calibration:** it builds the consolidated fee-adjusted book from `StatusReply.books`, replacing the runner's local books.
  - **Orders:** it submits the orders, then consumes `Subscribe`. Done means a terminal `OrderUpdate` has arrived for every order.
  - **Errors:** a feed process that exits with an error ends the run. The main process reports the root cause (as `root_cause` does today) and still prints the best-effort summary.
- **Replay and compare runs** use a single replay stream, sent from the main process, with the engine started as `--clock replay`. `compare` subscribes once for all its orders.
- **`record` is unchanged.** It does not use the engine.

## 6. Security

- **Unchanged:** loopback only, paper only, no keys.
- The stream adds no new data types beyond today's unary calls, and validation happens in the ingest thread before anything is queued.
- Live-mode clock spoofing is gone, which closes the review finding.
- Venue binding plus one stream per venue stops two feeders from corrupting a book.
- Every queue is bounded, and the replay mode flag is explicit and logged.
- Authentication (mTLS or tokens) is scoped to Phase 3 (multi-user). Until then, the engine refuses to bind to any non-loopback address (existing check).

## 7. Testing

- **C++ unit tests:**
  - queue ordering, and a single writer;
  - binding two venues concurrently, and rejecting a duplicate venue;
  - `RESOURCE_EXHAUSTED` on a full queue and on a slow subscriber;
  - live mode rejecting client time;
  - replay mode rejecting time that goes backwards or jumps more than a day;
  - a quiet-market timer tick triggering slices;
  - a closed stream leading to stale venues and orders halting at their deadline;
  - heartbeat freshness bounded at 30 s;
  - `seq` strictly increasing.
- **Concurrency:**
  - A new `SLIPSTREAM_TSAN=ON` build runs the stream and command tests under ThreadSanitizer in CI.
  - A stress test sends 100k events from two concurrent streams. The final books must equal a sequential replay of the same per-venue event sequences.
- **Python:** the aio client, the feed process lifecycle (clean exit, and a crash leading to a reported root cause), backpressure, and waiting for books, against local fake WebSocket servers and a fake engine.
- **End to end:** every existing replay integration test (routing, schedules, POV minimum size) runs through the new stream in replay mode with **unchanged expected numbers**. Plus a live two-venue demo check in the user's terminal before merge.
- **Benchmark:** `scripts/bench_pipeline.sh` runs as a CI step.
  - It replays a synthetic 50k-event stream against the release engine.
  - It reports p50, p99 and p99.9 send-to-fill latency and events per second.
  - It fails if p50 regresses by more than 2× against the committed baseline in `bench/baseline.json`.

## 8. Delivery (3 PRs)

1. **Engine.**
   - The stream and subscription RPCs, the engine thread and command queue, the clock modes, venue binding, engine-side freshness, stats, and C++ tests plus the ThreadSanitizer build.
   - The old unary calls still work and are routed through the command queue, so nothing breaks in between.
2. **Python.** The aio client, per-venue feed processes, the subscription consumer, waiting for books through `GetStatus`, and switching live, replay and compare to the stream. It also removes the runner's local books.
3. **Cleanup.** Remove `ApplyBookUpdate`, `ApplyTrades` and `Step`, add the CI benchmark and baseline, and update the README and `note.md` with measured latency.

## 9. Out of scope

- **Reconnect:** a dead feed ends the run, as today. Covered by sub-project 2.
- **Idempotent submit and cancel:** sub-project 3.
- **Consuming liquidity:** sub-project 4.
- **Checksums:** sub-project 5.
- Multiple subscribers, authentication, and more than one symbol per engine. These are all Phase 3.
