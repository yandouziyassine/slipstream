# Feed Reconnect and Liveness — Design

Date: 2026-09-28
Status: Approved in conversation (design points 1–7 below); concrete decisions are this spec's
Scope: Phase 2, sub-project 2 of `2026-09-26-low-latency-pipeline-design.md` ("a dead feed ends the
run"). Python only; engine changes this needs are listed in section 7 for the engine owner.

## 1. Purpose

Hourly collector runs last 10 minutes. Today one dropped exchange WebSocket ends the run and wastes
the hour. A venue's feed should survive a short outage, never trade on a book from before the
outage, and still stop safely when the outage is long.

## 2. Approved design

1. **Reconnect with backoff.** A dropped or failed WebSocket (network error, close, idle timeout)
   reconnects with exponential backoff and jitter, at most 5 reconnects per venue per run. The 6th
   failure ends the run, as a failure does today.
2. **Fresh snapshot after reconnect.** The venue's book is discarded and rebuilt only from the new
   connection's snapshot (a fresh parser, so Kraken checksums restart). The engine must not keep
   trading on the pre-disconnect book.
3. **Stale while down.** Routing uses the other venue through the engine's freshness rule.
4. **Fail safe when every venue is down.** No venue usable for more than 30 s stops the run.
5. **Accounting.** Every reconnect is logged (venue, attempt, reason, downtime) and stored per run.
6. **Kraken checksum mismatch** is decided below.
7. **Recorder** survives a reconnect with a marker line that replay understands; old recordings
   still replay.

## 3. Decisions

| Question | Decision | Why |
|---|---|---|
| What is retried | Network failures only: `OSError` (refused, reset, DNS, timeouts), any `websockets` exception (close, handshake, bad status), the idle timeout, and a Kraken checksum mismatch. Every other parser error and every engine error still ends the run at once | Malformed data means a broken or hostile peer: fail safe, never guess. Network loss is routine. |
| The initial connection | Retried like any other | A blip at the start of an hour wastes the hour just the same; the cap bounds a bad URL to ~15 s |
| Counting | Every failure counts, including a reconnect attempt that fails to connect. Failures 1–5 each trigger one reconnect; failure 6 ends the run with `gave up after 5 reconnects` and the last reason | "At most 5 reconnects" exactly; an outage can never loop |
| Backoff | Attempt *n* sleeps uniformly in `[d/2, d]`, `d = min(8 s, 0.5 s × 2^(n−1))` (0.5, 1, 2, 4, 8 s) | Equal jitter: spread out, yet never back to back. Worst case ~15.5 s of sleep per venue per run |
| Discarding the engine's book | On every failure the feed immediately sends an **empty snapshot** for its venue. The engine accepts it (a snapshot replaces the book; empty level lists are valid), so the venue has no book and no liquidity until the new connection's snapshot replaces it | No engine change. It acts at once instead of after `--stale-ms`, and it also works with one venue, where the engine never marks a venue stale. Heartbeats from the new connection cannot revive an old book, because there is none. |
| No update before the snapshot | Each connection gets a new `KrakenStream` / `CoinbaseStream`; both already reject a book update before their first snapshot | Reuses the per-connection parsers from the checksum work |
| Feed exit | A stopping feed (SIGTERM or error) also sends a best-effort empty snapshot before closing its stream | A later run on the same engine (the collector runs two sizes per hour) waits for new books instead of submitting on a frozen one; with one venue a frozen book never goes stale |
| Recovery | A venue has recovered when the first book snapshot of a new connection has been forwarded | That is when the venue can trade again |
| Downtime | Per reconnect: from that failure until recovery, or until the next failure, or until the feed stops. Each report says whether it recovered | Every reconnect gets exactly one row with a known downtime |
| Reports to the main process | A second `multiprocessing` queue next to the error queue carries `FeedReconnect(venue, attempt, reason, downtime_s, recovered)`. The supervisor validates each report (venue, ranges, lengths) and hands it to a callback; it drains the queue while waiting and once more after the feeds exit | Spawned feeds have no logging setup of their own; the main process logs to the collector's file |
| Checksum mismatch (point 6) | **Retried.** It counts as a reconnect and shares the cap. The mismatching message is never forwarded, the engine's book is emptied at once, and the new connection starts a new checksum mirror | Kraken's documented recovery for a mismatch is to resubscribe for a new snapshot. It cannot loop silently: it is logged with reason `BookChecksumError`, counted, and capped |
| All venues down (point 4) | After submit, the live session polls `GetStatus` every second. A venue is usable when it `has_book` and is `fresh`. No usable venue for more than 30 s ends the run with `LiveFeedError` | Uses only the engine's own view; no prices are guessed. With the empty-snapshot rule, a down venue has no book, which covers the single-venue case too |
| Recorder | Same policy and cap. On a failure it writes `{"recv_ns": N, "venue": V, "kind": "reconnect", "attempt": n, "reason": "..."}` and reconnects (resubscribing, so Kraken's subscribe ack is recorded again). It never sleeps past the recording deadline; an outage that reaches the deadline just ends the recording | Replays stay consistent with what the live feed saw |
| Replay of a marker | `read_replay` yields a `ReconnectMarker(recv_ns, venue)`; it validates `recv_ns` and `venue` and ignores the other fields. It also forgets the venue's Kraken checksum mirror until the next subscribe ack. The replay session sends an empty snapshot for that venue at the marker's time, forgets its local book (before submit) and starts a new Coinbase parser | "Discard this venue's book until the next snapshot". Files without markers replay exactly as before |
| Storage | Migration `002_feed_reconnects.sql`: table `feed_reconnects (run_id, source 'feed' or 'recorder', venue, attempt, reason, downtime_s, recovered)`, append-only like 001. The collector writes rows for completed and failed runs | One row per reconnect keeps the log and the count; `COUNT(*)` per run is the count |

## 4. Engine freshness today (point 3, read from `engine.cpp`)

- `fresh_locked`: with two or more venues, a venue is fresh while `now − alive ≤ --stale-ms`
  (default 2 s), where `alive` is the last book change, or the last heartbeat but at most 30 s past
  the last book change. With one venue it is always fresh.
- Stale venues are left out of the consolidated mid and of routing, so routing uses the other venue.
  No engine change is needed for point 3.

## 5. All venues stale today (point 4)

With no fresh venue the consolidated mid is unknown, so `advance_locked` does nothing: orders
**pause**, stay `WORKING`, and halt with `deadline reached` at their deadline. They do not halt
early. The schedule's target keeps growing while paused, so when a venue comes back the next child
catches up the whole backlog at once. The Python watch in section 3 ends the run, and the feeds
empty their books as they stop, so no order of that run can trade again in this run. Section 7
lists the engine change that makes the stop exact.

## 6. Testing

Fake WebSocket servers (`websockets.asyncio.server.serve`) and an injected sleep, clock and random
source, so no test sleeps for real backoff:
- a drop mid-stream, then reconnect, snapshot, continue; the engine's book is emptied in between;
- backoff delays for attempts 1–5; the 6th failure ends with a clear error;
- no update applied before the new snapshot; a checksum mismatch reconnects;
- reconnects reported, validated, logged, and stored per run; the stale watch ends a run;
- recorder marker, replay of a marker, and old recordings unchanged.

## 7. Engine changes requested (not in this PR)

1. **Halt orders when no venue is usable.** In `Engine::step`, track the last time any venue had a
   book and was fresh. When none has been for more than 30 s (`kNoMarketHaltNs = 30 s`), set every
   working order to `Halted` with reason `no fresh market data for 30s`. This replaces the backlog
   catch-up with a hard stop, independent of the Python watch.
2. **Cancel** (sub-project 3): a run that ends early cannot remove its working orders from a shared
   engine; they stay working until their deadline and can trade on the next run's data.
