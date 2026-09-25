# Smart Routing Integration (PR 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the multi-venue engine (PR #11) and the multi-venue Python feeds (PR #12) together. `slipstream live|replay|compare|record --venues kraken,coinbase` should route every child across both venues and report fees, routed all-in cost, each venue's all-in cost, and the gain versus the best single venue.

**Architecture:**
- The engine is the single source of truth for venues and fees.
  - `GetStatus` now reports each registered venue with its fee.
  - Python reads that list back, refuses to run when it differs from `--venues`, and uses the fees only to calibrate η on the consolidated fee-adjusted book.
- The runner stamps every `BookUpdate` with its receive time (`recv_ns`), which the engine's staleness check needs.
- The Coinbase parser keeps a full-book mirror and sends the engine a top-N view, because the engine keeps only the top N levels. This fixes a book-erosion bug (Task 2).

**Tech Stack:** C++20 + gRPC/protobuf (engine), Python 3.12 (grpcio, websockets, pytest, mypy --strict, ruff).

**Spec:** `docs/superpowers/specs/2026-09-24-smart-routing-design.md`. Sections 4.1, 4.2 and 4.4 are amended in the same commit as this plan.

**Rules for every task (CLAUDE.md):**
- Work test-first: red, then green, then refactor.
- After each task, run `/security-review` and `/caveman:caveman-review` on that task's diff.
- Fix every valid finding test-first before starting the next task.
- Commit authorship uses the GitHub noreply address, set through the `GIT_AUTHOR_EMAIL` and `GIT_COMMITTER_EMAIL` env vars. Never write it to git config.
- Every commit ends with the `Co-Authored-By` trailer for the model that wrote it.

**Build/test commands** (WSL Ubuntu 24.04, from the worktree root `.worktrees/routing-integration`):
- Protobuf stubs: `bash scripts/gen_proto.sh`
- Engine: `bash scripts/build_engine.sh && ctest --test-dir build/engine --output-on-failure`
- Python: `cd python && pytest -q && ruff check . && ruff format --check . && mypy slipstream`
- Everything: `bash scripts/ci.sh` (must print `CI OK`)

---

## Background the engineer needs

- **Venues.** The engine registers venues with `--venue NAME:fee_bps=F` (allowlist `kraken`, `coinbase`). With no flag, the only venue is `kraken:0`.
  - With 2 or more venues, a venue whose last `BookUpdate.recv_ns` is older than `--stale-ms` (default 2000) counts as stale. Stale venues are excluded from routing and from the consolidated mid.
  - **Today the Python client never sends `recv_ns` (it stays 0).** A two-venue engine therefore sees every venue as stale and never fills. Task 3 and Task 4 fix this.
- **Engine books** (`engine/src/order_book.cpp`) keep at most `book_depth` levels per side (default 10). They truncate after every snapshot and after every delta.
  - Kraken's `book` channel maintains that depth window on the server, sending the level that enters the window whenever one leaves. Engine truncation is therefore harmless for Kraken.
  - Coinbase `level2` sends deltas for the **whole** book. When a top level is deleted, the engine has already dropped the deeper level that should replace it, so the Coinbase book thins out over a session.
  - Task 2 fixes this in Python: the parser keeps the full book and emits a top-N snapshot on every level2 message.
  - That also closes the PR 2 review item "chunk Coinbase updates to ≤1000 levels". A top-N view has at most 100 levels, and the engine rejects anything over 1000 with `INVALID_ARGUMENT`, which surfaces as `EngineError`.
- **Routing results** come back in each `Fill` (`venue` and `fee`) and in each `OrderStatus`:
  - `fees_paid` and `fees_bps`;
  - `routed_all_in_bps`;
  - `venue_costs[] {venue, all_in_bps, available}`.
  - "Gain vs best single venue" is computed in Python as `min(available all_in_bps) − routed_all_in_bps`.

## File map

| File | Change |
|---|---|
| `proto/slipstream/v1/execution.proto` | `VenueInfo`; `StatusReply.venues = 5` |
| `engine/src/engine.{h,cpp}` | `Venue::fee_bps`; `venue_settings()` |
| `engine/src/service.cpp` | `GetStatus` fills `venues` |
| `engine/tests/service_test.cpp` | Status lists venues and fees |
| `python/slipstream/models.py` | `VENUES` tuple; `Fill.venue`, `Fill.fee` |
| `python/slipstream/coinbase.py` | `_BookSide` full-book mirror; top-N snapshot per message |
| `python/slipstream/engine_client.py` | `recv_ns` and `venue` on requests; `Fill` venue and fee; `venue_fees()` |
| `python/slipstream/book.py` (new) | `LocalBook`, `consolidated_book` |
| `python/slipstream/runner.py` | Per-venue `LocalBook`s; consolidated calibration book; `recv_ns`; `fee_bps`; `venues` property |
| `python/slipstream/replay.py` | Skip records from unconfigured venues; use `VENUES` |
| `python/slipstream/cli.py` | `--venues`; engine venue check; report columns |
| `python/tests/conftest.py` | `FakeEngine.apply_book(update, recv_ns)`; two-venue engine fixture |
| `python/tests/fixtures/two_venue_btcusd_replay.jsonl` (new) | Hand-derived two-venue fixture |
| `python/tests/test_integration_routing.py` (new) | End-to-end routing through the real engine |
| `scripts/demo_live.sh`, `scripts/demo_compare.sh` | Two venues, illustrative fees |
| `README.md`, `note.md` | Routing docs, live result, dev log |

---

### Task 1: Engine reports venues and fees in `GetStatus`

**Files:**
- Modify: `proto/slipstream/v1/execution.proto`
- Modify: `engine/src/engine.h`, `engine/src/engine.cpp`, `engine/src/service.cpp`
- Test: `engine/tests/service_test.cpp`

- [ ] **Step 1: Write the failing test.** Append to `engine/tests/service_test.cpp`:

```cpp
TEST(ServiceMultiVenue, StatusListsVenuesAndFeesInRegistrationOrder) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10, {{"kraken", 40.0}, {"coinbase", 60.0}},
                  2'000'000'000);
    ExecutionService service{engine, "BTC/USD"};
    v1::StatusRequest request;
    v1::StatusReply status;
    ASSERT_TRUE(service.GetStatus(nullptr, &request, &status).ok());
    ASSERT_EQ(status.venues_size(), 2);
    EXPECT_EQ(status.venues(0).name(), "kraken");
    EXPECT_DOUBLE_EQ(status.venues(0).fee_bps(), 40.0);
    EXPECT_EQ(status.venues(1).name(), "coinbase");
    EXPECT_DOUBLE_EQ(status.venues(1).fee_bps(), 60.0);
}

TEST(ServiceMultiVenue, DefaultEngineListsKrakenWithZeroFee) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10);
    ExecutionService service{engine, "BTC/USD"};
    v1::StatusRequest request;
    v1::StatusReply status;
    ASSERT_TRUE(service.GetStatus(nullptr, &request, &status).ok());
    ASSERT_EQ(status.venues_size(), 1);
    EXPECT_EQ(status.venues(0).name(), "kraken");
    EXPECT_DOUBLE_EQ(status.venues(0).fee_bps(), 0.0);
}
```

- [ ] **Step 2: Run it and confirm it fails.** Run `bash scripts/build_engine.sh`. Expected: a compile error, "no member named 'venues_size'".

- [ ] **Step 3: Implement.**

In `proto/slipstream/v1/execution.proto`, add a message before `StatusReply` and a field to `StatusReply`:

```proto
message VenueInfo {
  string name = 1;
  double fee_bps = 2;
}

message StatusReply {
  double position = 1;
  bool has_mid = 2;
  double mid = 3;
  repeated OrderStatus orders = 4;
  repeated VenueInfo venues = 5;
}
```

In `engine/src/engine.h`:
- Add a public method next to `venue_count()`: `std::vector<VenueSettings> venue_settings() const;`
- Add a `fee_bps` field to the private `struct Venue`, keeping this field order:

```cpp
    struct Venue {
        std::string name;
        double fee_bps;
        double fee_rate;
        OrderBook book;
        std::int64_t last_update_ns;
    };
```

In `engine/src/engine.cpp`, the constructor keeps the configured bps so it is reported exactly, with no `fee_rate * 1e4` round trip:

```cpp
    for (auto& venue : venues) {
        const double fee_bps = venue.fee_bps;
        venues_.push_back(Venue{std::move(venue.name), fee_bps, fee_bps / 1e4,
                                OrderBook(book_depth), 0});
    }
```

Add the accessor after `venue_count()`:

```cpp
std::vector<VenueSettings> Engine::venue_settings() const {
    std::lock_guard lock(mu_);
    std::vector<VenueSettings> out;
    out.reserve(venues_.size());
    for (const auto& venue : venues_) out.push_back({venue.name, venue.fee_bps});
    return out;
}
```

In `engine/src/service.cpp` `GetStatus`, add this after the orders loop and before `return`:

```cpp
    for (const auto& venue : engine_.venue_settings()) {
        auto* info = reply->add_venues();
        info->set_name(venue.name);
        info->set_fee_bps(venue.fee_bps);
    }
```

- [ ] **Step 4: Run the tests and confirm they pass.** Run `bash scripts/build_engine.sh && ctest --test-dir build/engine --output-on-failure`. Expected: every C++ test passes (106 = 104 + 2).

- [ ] **Step 5: Regenerate the Python stubs and run the Python tests.** Run `bash scripts/gen_proto.sh && (cd python && pytest -q)`. Expected: everything passes, since the proto change is additive.

- [ ] **Step 6: Commit.**

```bash
git add proto/slipstream/v1/execution.proto engine/src/engine.h engine/src/engine.cpp engine/src/service.cpp engine/tests/service_test.cpp
git commit -m "feat(engine): report registered venues and their fees in GetStatus"
```

- [ ] **Step 7: Review.** Run `/security-review` and `/caveman:caveman-review` on `HEAD~1..HEAD`. Fix valid findings test-first and commit.

---

### Task 2: Coinbase parser mirrors the full book and emits a top-N view

**Files:**
- Modify: `python/slipstream/coinbase.py`
- Test: `python/tests/test_coinbase.py`

- [ ] **Step 1: Write the failing tests.** Replace `test_update_passes_deltas_through` in `python/tests/test_coinbase.py` with the tests below. Keep `test_snapshot_is_sorted_truncated_and_tagged` as is; it still holds.

```python
def test_update_emits_top_of_book_view() -> None:
    stream = CoinbaseStream("BTC/USD", depth=10)
    stream.parse(l2(0, "snapshot", [upd("bid", "99", "1"), upd("offer", "101", "1")]))
    assert stream.parse(l2(1, "update", [upd("bid", "99", "0"), upd("offer", "100.9", "4")])) == (
        BookUpdate("BTC/USD", True, (), ((100.9, 4.0), (101.0, 1.0)), "coinbase")
    )


def test_deleting_a_top_level_brings_the_next_level_into_view() -> None:
    stream = CoinbaseStream("BTC/USD", depth=2)
    stream.parse(
        l2(
            0,
            "snapshot",
            [
                upd("bid", "99", "1"),
                upd("bid", "98", "1"),
                upd("bid", "97", "1"),
                upd("offer", "101", "1"),
                upd("offer", "102", "1"),
                upd("offer", "103", "1"),
            ],
        )
    )
    view = stream.parse(l2(1, "update", [upd("offer", "101", "0"), upd("bid", "99", "0")]))
    assert view == BookUpdate(
        "BTC/USD", True, ((98.0, 1.0), (97.0, 1.0)), ((102.0, 1.0), (103.0, 1.0)), "coinbase"
    )


def test_update_before_snapshot_raises() -> None:
    with pytest.raises(CoinbaseMessageError, match="before snapshot"):
        CoinbaseStream("BTC/USD", depth=10).parse(l2(0, "update", [upd("bid", "99", "1")]))


def test_new_snapshot_replaces_the_book() -> None:
    stream = CoinbaseStream("BTC/USD", depth=10)
    stream.parse(l2(0, "snapshot", [upd("bid", "99", "1"), upd("offer", "101", "1")]))
    view = stream.parse(l2(1, "snapshot", [upd("bid", "50", "2"), upd("offer", "60", "3")]))
    assert view == BookUpdate("BTC/USD", True, ((50.0, 2.0),), ((60.0, 3.0),), "coinbase")


def test_book_level_cap_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("slipstream.coinbase.MAX_BOOK_LEVELS", 2)
    stream = CoinbaseStream("BTC/USD", depth=10)
    stream.parse(l2(0, "snapshot", [upd("bid", "99", "1"), upd("bid", "98", "1")]))
    with pytest.raises(CoinbaseMessageError, match="too many levels"):
        stream.parse(l2(1, "update", [upd("bid", "97", "1")]))


def test_oversized_snapshot_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("slipstream.coinbase.MAX_BOOK_LEVELS", 1)
    with pytest.raises(CoinbaseMessageError, match="too many levels"):
        CoinbaseStream("BTC/USD", depth=10).parse(
            l2(0, "snapshot", [upd("bid", "99", "1"), upd("bid", "98", "1")])
        )
```

- [ ] **Step 2: Run the tests and confirm they fail.** Run `cd python && pytest tests/test_coinbase.py -q`. Expected: the new tests fail; the update returns `is_snapshot=False` deltas and nothing raises "before snapshot".

- [ ] **Step 3: Implement.**

In `python/slipstream/coinbase.py`:
- Add `from bisect import bisect_left, insort` to the imports.
- Add `MAX_BOOK_LEVELS = 200_000` next to `MAX_UPDATES`.
- Add this class above `CoinbaseStream`:

```python
class _BookSide:
    """One side of the full Coinbase book, with prices kept sorted best-first."""

    def __init__(self, descending: bool) -> None:
        self._sign = -1.0 if descending else 1.0
        self._qty: dict[float, float] = {}
        self._keys: list[float] = []

    def replace(self, levels: list[tuple[float, float]]) -> None:
        qty = {price: size for price, size in levels if size > 0}
        if len(qty) > MAX_BOOK_LEVELS:
            raise CoinbaseMessageError("book has too many levels")
        self._qty = qty
        self._keys = sorted(self._sign * price for price in qty)

    def set(self, price: float, qty: float) -> None:
        key = self._sign * price
        if qty == 0:
            if self._qty.pop(price, None) is not None:
                del self._keys[bisect_left(self._keys, key)]
            return
        if price not in self._qty:
            if len(self._qty) >= MAX_BOOK_LEVELS:
                raise CoinbaseMessageError("book has too many levels")
            insort(self._keys, key)
        self._qty[price] = qty

    def top(self, depth: int) -> tuple[tuple[float, float], ...]:
        return tuple((self._sign * key, self._qty[self._sign * key]) for key in self._keys[:depth])
```

In `CoinbaseStream.__init__`, add:

```python
        self._bids = _BookSide(descending=True)
        self._asks = _BookSide(descending=False)
        self._has_book = False
```

Replace the tail of `_book` (from `is_snapshot = kinds == {"snapshot"}` to the `return`) with the code below. Field validation still runs first, so the hostile-input tests keep exercising it:

```python
        # Coinbase sends full-book deltas, but the engine keeps only the top N levels, so it could
        # never refill a deleted top level. Keep the whole book here and send the top-N view.
        if kinds == {"snapshot"}:
            self._bids.replace(bids)
            self._asks.replace(asks)
            self._has_book = True
        else:
            if not self._has_book:
                raise CoinbaseMessageError("level2 update before snapshot")
            for price, qty in bids:
                self._bids.set(price, qty)
            for price, qty in asks:
                self._asks.set(price, qty)
        return BookUpdate(
            self._symbol,
            True,
            self._bids.top(self._depth),
            self._asks.top(self._depth),
            "coinbase",
        )
```

Leave `MAX_DEPTH = 1000` unchanged; it already equals the engine's per-update cap, `kMaxLevelsPerUpdate`, so no top-N view can exceed it.

- [ ] **Step 4: Run the tests and confirm they pass.** Run `cd python && pytest tests/test_coinbase.py tests/test_runner.py tests/test_recorder.py tests/test_live.py -q`. Expected: everything passes. If a runner, recorder, or live test sent a Coinbase `update` before any snapshot, prepend a snapshot in that test; this is the new fail-safe contract.

- [ ] **Step 5: Lint, type-check, and commit.**

```bash
(cd python && ruff check . && ruff format --check . && mypy slipstream)
git add python/slipstream/coinbase.py python/tests/test_coinbase.py
git commit -m "fix(orchestrator): mirror the full Coinbase book so deleted top levels are refilled"
```

- [ ] **Step 6: Review.** Run `/security-review` and `/caveman:caveman-review` on this task's diff. Fix valid findings test-first.

---

### Task 3: Engine client carries venue, `recv_ns`, and fees

**Files:**
- Modify: `python/slipstream/models.py`, `python/slipstream/engine_client.py`, `python/slipstream/runner.py`, `python/slipstream/replay.py`
- Test: `python/tests/test_engine_client.py`

- [ ] **Step 1: Write the failing tests.** In `python/tests/test_engine_client.py`, replace `test_book_update_to_proto` and append the new tests:

```python
def test_book_update_to_proto() -> None:
    update = BookUpdate("BTC/USD", True, ((99.0, 1.0),), ((101.0, 2.0),), "coinbase")
    msg = book_update_to_proto(update, recv_ns=123)
    assert msg.symbol == "BTC/USD"
    assert msg.is_snapshot
    assert msg.venue == "coinbase"
    assert msg.recv_ns == 123
    assert [(level.price, level.qty) for level in msg.bids] == [(99.0, 1.0)]
    assert [(level.price, level.qty) for level in msg.asks] == [(101.0, 2.0)]


def test_trade_batch_to_proto_carries_venue() -> None:
    msg = trade_batch_to_proto(TradeBatch("BTC/USD", False, ((100.0, 0.5),), "coinbase"))
    assert msg.venue == "coinbase"
    assert [(t.price, t.qty) for t in msg.trades] == [(100.0, 0.5)]


def fake_status(*venues: tuple[str, float]) -> pb.StatusReply:
    return pb.StatusReply(venues=[pb.VenueInfo(name=n, fee_bps=f) for n, f in venues])


def test_venue_fees_reads_engine_venues(monkeypatch: pytest.MonkeyPatch) -> None:
    client = EngineClient("127.0.0.1:1")
    try:
        monkeypatch.setattr(
            client, "status", lambda: fake_status(("kraken", 40.0), ("coinbase", 60.0))
        )
        assert client.venue_fees() == {"kraken": 40.0, "coinbase": 60.0}
    finally:
        client.close()


@pytest.mark.parametrize(
    "venues",
    [(), (("binance", 10.0),), (("kraken", float("nan")),), (("kraken", -1.0),)],
    ids=["none", "unknown", "nan", "negative"],
)
def test_venue_fees_rejects_bad_engine_venues(
    monkeypatch: pytest.MonkeyPatch, venues: tuple[tuple[str, float], ...]
) -> None:
    client = EngineClient("127.0.0.1:1")
    try:
        monkeypatch.setattr(client, "status", lambda: fake_status(*venues))
        with pytest.raises(EngineError):
            client.venue_fees()
    finally:
        client.close()


def test_fill_defaults_keep_single_venue_callers_working() -> None:
    fill = Fill("o", 1, 0.5, 100.0)
    assert (fill.venue, fill.fee) == ("kraken", 0.0)
```

Add `Fill` to the `slipstream.models` import in that test file.

- [ ] **Step 2: Run the tests and confirm they fail.** Run `cd python && pytest tests/test_engine_client.py -q`. Expected: `TypeError` for the unexpected `recv_ns` argument, and `AttributeError` for `venue_fees`.

- [ ] **Step 3: Implement.**

In `python/slipstream/models.py`, add `VENUES` after the `Venue` Literal:

```python
VENUES: tuple[Venue, ...] = ("kraken", "coinbase")
```

Extend `Fill`:

```python
@dataclass(frozen=True)
class Fill:
    order_id: str
    ts_ns: int
    qty: float
    price: float
    venue: str = "kraken"
    fee: float = 0.0
```

In `python/slipstream/engine_client.py`, add `import math`, add `VENUES` and `Venue` to the models import, and change the following:

```python
def book_update_to_proto(update: BookUpdate, recv_ns: int) -> pb.BookUpdate:
    return pb.BookUpdate(
        symbol=update.symbol,
        is_snapshot=update.is_snapshot,
        bids=[pb.PriceLevel(price=price, qty=qty) for price, qty in update.bids],
        asks=[pb.PriceLevel(price=price, qty=qty) for price, qty in update.asks],
        venue=update.venue,
        recv_ns=recv_ns,
    )


def trade_batch_to_proto(batch: TradeBatch) -> pb.TradeBatch:
    return pb.TradeBatch(
        symbol=batch.symbol,
        trades=[pb.Trade(price=price, qty=qty) for price, qty in batch.trades],
        venue=batch.venue,
    )
```

Change these `EngineClient` methods:

```python
    def apply_book(self, update: BookUpdate, recv_ns: int) -> None:
        self._call(self._stub.ApplyBookUpdate, book_update_to_proto(update, recv_ns))

    def step(self, now_ns: int) -> list[Fill]:
        reply = cast(pb.StepReply, self._call(self._stub.Step, pb.StepRequest(now_ns=now_ns)))
        return [Fill(f.order_id, f.ts_ns, f.qty, f.price, f.venue, f.fee) for f in reply.fills]

    def venue_fees(self) -> dict[Venue, float]:
        fees: dict[Venue, float] = {}
        for info in self.status().venues:
            if info.name not in VENUES or not math.isfinite(info.fee_bps) or info.fee_bps < 0:
                raise EngineError(f"engine reported an invalid venue {info.name[:32]!r}")
            fees[cast(Venue, info.name)] = info.fee_bps
        if not fees:
            raise EngineError("engine reported no venues")
        return fees
```

In `python/slipstream/runner.py` and `python/slipstream/replay.py`, replace the local `_VALID_VENUES` frozensets with `frozenset(VENUES)` imported from `slipstream.models`.

In `runner.py`, update the `Engine` Protocol, and make the matching call pass the receive time:

```python
class Engine(Protocol):
    def apply_book(self, update: BookUpdate, recv_ns: int) -> None: ...
```

```python
            self._engine.apply_book(update, now_ns)
```

In `python/tests/conftest.py`, change `FakeEngine`: add `self.book_recv_ns: list[int] = []` in `__init__`, and use this method:

```python
    def apply_book(self, update: BookUpdate, recv_ns: int) -> None:
        self.books.append(update)
        self.book_recv_ns.append(recv_ns)
```

Add a runner test to `python/tests/test_runner.py`:

```python
def test_book_updates_carry_their_receive_time(fake_engine: FakeEngine) -> None:
    runner = make_runner(fake_engine)
    runner.on_message(snapshot(), 1234)
    assert fake_engine.book_recv_ns == [1234]
```

- [ ] **Step 4: Run the tests and confirm they pass.** Run `cd python && pytest -q && mypy slipstream`. Expected: everything passes, including both real-engine integration tests.

- [ ] **Step 5: Commit.**

```bash
git add python/slipstream/models.py python/slipstream/engine_client.py python/slipstream/runner.py python/slipstream/replay.py python/tests/conftest.py python/tests/test_engine_client.py python/tests/test_runner.py
git commit -m "feat(orchestrator): send venue and receive time to the engine and read back fills' venue and fee"
```

- [ ] **Step 6: Review.** Run `/security-review` and `/caveman:caveman-review` on this task's diff. Fix valid findings test-first.

---

### Task 4: Runner keeps per-venue books and calibrates on the consolidated fee-adjusted book

**Files:**
- Create: `python/slipstream/book.py`
- Modify: `python/slipstream/runner.py`
- Test: `python/tests/test_book.py` (new), `python/tests/test_runner.py`

- [ ] **Step 1: Write the failing tests.** Create `python/tests/test_book.py`:

```python
import pytest

from slipstream.book import LocalBook, consolidated_book
from slipstream.models import BookUpdate


def test_snapshot_then_deltas_keep_top_depth() -> None:
    book = LocalBook(depth=2)
    book.apply(BookUpdate("BTC/USD", True, ((99.0, 1.0), (98.0, 1.0)), ((101.0, 1.0),)))
    book.apply(BookUpdate("BTC/USD", False, ((99.5, 2.0), (98.0, 0.0)), ((102.0, 3.0),)))
    assert book.bids() == ((99.5, 2.0), (99.0, 1.0))
    assert book.asks() == ((101.0, 1.0), (102.0, 3.0))
    book.apply(BookUpdate("BTC/USD", False, ((97.0, 1.0),), ()))
    assert book.bids() == ((99.5, 2.0), (99.0, 1.0))


def test_snapshot_replaces_everything() -> None:
    book = LocalBook(depth=10)
    book.apply(BookUpdate("BTC/USD", True, ((99.0, 1.0),), ((101.0, 1.0),)))
    book.apply(BookUpdate("BTC/USD", True, ((50.0, 1.0),), ()))
    assert book.bids() == ((50.0, 1.0),)
    assert book.asks() == ()


def test_depth_must_be_positive() -> None:
    with pytest.raises(ValueError):
        LocalBook(depth=0)


def test_consolidated_book_merges_at_fee_adjusted_prices() -> None:
    kraken = LocalBook(depth=10)
    kraken.apply(BookUpdate("BTC/USD", True, ((99.0, 1.0),), ((100.0, 1.0), (100.3, 2.0))))
    coinbase = LocalBook(depth=10)
    coinbase.apply(BookUpdate("BTC/USD", True, ((99.2, 1.0),), ((100.2, 5.0),), "coinbase"))
    merged = consolidated_book(
        "BTC/USD", {"kraken": kraken, "coinbase": coinbase}, {"kraken": 0.0, "coinbase": 20.0}
    )
    # asks: kraken 100.0 and 100.3 unchanged; coinbase 100.2 x 1.002 = 100.4004 ranks last.
    assert merged.asks == pytest.approx(((100.0, 1.0), (100.3, 2.0), (100.4004, 5.0)))
    # bids: coinbase 99.2 x 0.998 = 99.0016 ranks above kraken 99.0.
    assert merged.bids == pytest.approx(((99.0016, 1.0), (99.0, 1.0)))
```

Add to `python/tests/test_runner.py` (add `BookUpdate` and `ScheduleParams` to the `slipstream.models` import). `snapshot()` is Kraken 99×1 / 101×1 and `cb_snapshot()` is Coinbase 99×1 / 101×1, both already in the file. With Coinbase at 100 bps, its ask becomes 101 × 1.01 = 102.01 and its bid becomes 99 × 0.99 = 98.01:

```python
def test_calibration_uses_the_consolidated_fee_adjusted_book(
    fake_engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[BookUpdate] = []

    def spy(
        spec: OrderSpec, book: BookUpdate, start_ns: int, data: CalibrationData | None
    ) -> ScheduleParams:
        seen.append(book)
        return TwapParams()

    monkeypatch.setattr("slipstream.runner.schedule_params", spy)
    runner = ExecutionRunner(
        fake_engine,
        SPEC,
        "BTC/USD",
        logging.getLogger("t"),
        venues=("kraken", "coinbase"),
        fee_bps={"kraken": 0.0, "coinbase": 100.0},
    )
    runner.on_message(snapshot(), 100, "kraken")
    runner.on_message(cb_snapshot(), 200, "coinbase")
    (book,) = seen
    assert book.asks == pytest.approx(((101.0, 1.0), (102.01, 1.0)))
    assert book.bids == pytest.approx(((99.0, 1.0), (98.01, 1.0)))


def test_calibration_book_includes_deltas_since_the_first_snapshot(
    fake_engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[BookUpdate] = []

    def spy(
        spec: OrderSpec, book: BookUpdate, start_ns: int, data: CalibrationData | None
    ) -> ScheduleParams:
        seen.append(book)
        return TwapParams()

    monkeypatch.setattr("slipstream.runner.schedule_params", spy)
    runner = ExecutionRunner(
        fake_engine, SPEC, "BTC/USD", logging.getLogger("t"), venues=("kraken", "coinbase")
    )
    runner.on_message(snapshot(), 100, "kraken")
    delta = {
        "channel": "book",
        "type": "update",
        "data": [{"symbol": "BTC/USD", "bids": [], "asks": [{"price": 100.5, "qty": 2.0}]}],
    }
    runner.on_message(json.dumps(delta), 150, "kraken")
    runner.on_message(cb_snapshot(), 200, "coinbase")
    (book,) = seen
    assert book.asks == ((100.5, 2.0), (101.0, 1.0), (101.0, 1.0))
```

If the Kraken parser rejects that delta (for example because it requires `checksum` or `timestamp`), add the missing fields using the same shape as the `update` lines in `python/tests/fixtures/kraken_btcusd_replay.jsonl`. Do not change the asserted levels.

```python
def test_fill_log_carries_venue_and_fee(
    fake_engine: FakeEngine, caplog: pytest.LogCaptureFixture
) -> None:
    fake_engine.fills_per_step = [[Fill("o-1", 5, 0.25, 101.0, "coinbase", 0.02)]]
    runner = make_runner(fake_engine)
    with caplog.at_level(logging.INFO):
        runner.on_message(snapshot(), 5)
    fields = [r.fields for r in caplog.records if getattr(r, "fields", {}).get("event") == "fill"]
    assert fields[0]["venue"] == "coinbase"
    assert fields[0]["fee"] == 0.02


def test_runner_exposes_its_venues(fake_engine: FakeEngine) -> None:
    runner = ExecutionRunner(
        fake_engine, SPEC, "BTC/USD", logging.getLogger("t"), venues=("coinbase", "kraken")
    )
    assert runner.venues == frozenset({"kraken", "coinbase"})
```

- [ ] **Step 2: Run the tests and confirm they fail.** Run `cd python && pytest tests/test_book.py tests/test_runner.py -q`. Expected: `ModuleNotFoundError: slipstream.book`, and runner tests fail on the unexpected `fee_bps` argument.

- [ ] **Step 3: Implement.** Create `python/slipstream/book.py`:

```python
from __future__ import annotations

from collections.abc import Mapping

from slipstream.models import BookUpdate, Venue


class LocalBook:
    """One venue's book as the engine holds it: snapshots replace, deltas upsert, top N kept."""

    def __init__(self, depth: int) -> None:
        if depth < 1:
            raise ValueError("depth must be at least 1")
        self._depth = depth
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}

    def apply(self, update: BookUpdate) -> None:
        if update.is_snapshot:
            self._bids.clear()
            self._asks.clear()
        for side, levels in ((self._bids, update.bids), (self._asks, update.asks)):
            for price, qty in levels:
                if qty == 0:
                    side.pop(price, None)
                else:
                    side[price] = qty
        self._bids = dict(self.bids())
        self._asks = dict(self.asks())

    def bids(self) -> tuple[tuple[float, float], ...]:
        return tuple(sorted(self._bids.items(), reverse=True)[: self._depth])

    def asks(self) -> tuple[tuple[float, float], ...]:
        return tuple(sorted(self._asks.items())[: self._depth])


def consolidated_book(
    symbol: str, books: Mapping[Venue, LocalBook], fee_bps: Mapping[Venue, float]
) -> BookUpdate:
    """All venues' levels at fee-adjusted prices, ranked the way the engine's router ranks them."""
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for venue, book in books.items():
        fee = fee_bps.get(venue, 0.0) / 1e4
        bids.extend((price * (1 - fee), qty) for price, qty in book.bids())
        asks.extend((price * (1 + fee), qty) for price, qty in book.asks())
    return BookUpdate(symbol, True, tuple(sorted(bids, reverse=True)), tuple(sorted(asks)))
```

In `python/slipstream/runner.py`:
- Import `Mapping` from `collections.abc`, and `LocalBook` and `consolidated_book` from `slipstream.book`.
- Add the constructor parameter `fee_bps: Mapping[Venue, float] | None = None` after `book_depth`.
- Store it: `self._fee_bps: dict[Venue, float] = dict(fee_bps or {})`.
- Store the books: `self._books: dict[Venue, LocalBook] = {venue: LocalBook(book_depth) for venue in self._venues}`.
- Add the property:

```python
    @property
    def venues(self) -> frozenset[Venue]:
        return self._venues
```

Replace the `BookUpdate` branch of `on_message`:

```python
        if isinstance(update, BookUpdate):
            self._check_symbol(update.symbol)
            self._engine.apply_book(update, now_ns)
            self._books[venue].apply(update)
            if update.is_snapshot and not self._submitted:
                self._snapshot_venues.add(venue)
                if self._snapshot_venues >= self._venues:
                    self._submit_all(now_ns)
```

Change `_submit_all` to build the book itself:

```python
    def _submit_all(self, now_ns: int) -> None:
        book = consolidated_book(self._symbol, self._books, self._fee_bps)
        planned = [
            (spec, schedule_params(spec, book, now_ns, self._calibration)) for spec in self._specs
        ]
```

(The rest of `_submit_all` is unchanged.)

In `_step`, add `"venue": fill.venue` and `"fee": fill.fee` to the fill log `fields`.

- [ ] **Step 4: Run the tests and confirm they pass.** Run `cd python && pytest -q && mypy slipstream && ruff check .`. Expected: everything passes. A single venue with zero fees gives a consolidated book equal to that venue's book, so the existing calibration tests are unchanged.

- [ ] **Step 5: Commit.**

```bash
git add python/slipstream/book.py python/slipstream/runner.py python/tests/test_book.py python/tests/test_runner.py
git commit -m "feat(orchestrator): calibrate impact on the consolidated fee-adjusted book across venues"
```

- [ ] **Step 6: Review.** Run `/security-review` and `/caveman:caveman-review` on this task's diff. Fix valid findings test-first.

---

### Task 5: `--venues` on the CLI, engine venue check, and replay venue filter

**Files:**
- Modify: `python/slipstream/cli.py`, `python/slipstream/replay.py`
- Test: `python/tests/test_cli.py`, `python/tests/test_replay.py`

- [ ] **Step 1: Write the failing tests.** Append to `python/tests/test_cli.py`:

```python
@pytest.mark.parametrize("value", ["", "binance", "kraken,binance", ","])
def test_rejects_bad_venues(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([*BASE, "--qty", "1", "--venues", value])


def test_parses_and_dedupes_venues() -> None:
    args = build_parser().parse_args([*BASE, "--qty", "1", "--venues", "coinbase, kraken,coinbase"])
    assert args.venues == ("coinbase", "kraken")


def test_venues_default_to_kraken() -> None:
    assert build_parser().parse_args([*BASE, "--qty", "1"]).venues == ("kraken",)


@pytest.mark.parametrize("command", ["live", "compare", "record"])
def test_every_feed_command_accepts_venues(command: str) -> None:
    base = {
        "live": ["live", "--side", "buy", "--qty", "1", "--duration", "6", "--slices", "3"],
        "compare": ["compare", "--side", "buy", "--qty", "1", "--duration", "6", "--slices", "3"],
        "record": ["record", "--duration", "5", "--out", "x.jsonl"],
    }[command]
    assert build_parser().parse_args([*base, "--venues", "kraken,coinbase"]).venues == (
        "kraken",
        "coinbase",
    )


class _FakeClient:
    def __init__(self, address: str) -> None:
        self.closed = False

    def wait_ready(self) -> None:
        pass

    def venue_fees(self) -> dict[str, float]:
        return {"kraken": 40.0}

    def close(self) -> None:
        self.closed = True


def test_venue_mismatch_with_engine_is_refused(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("SLIPSTREAM_PAPER_MODE", raising=False)
    monkeypatch.setattr("slipstream.cli.EngineClient", _FakeClient)
    assert main([*BASE, "--qty", "1", "--venues", "kraken,coinbase"]) == 1
    assert "do not match" in caplog.text
```

Append to `python/tests/test_replay.py`. The file already has `SNAPSHOT` (a Kraken snapshot dict), `coinbase_snapshot(seq)`, and the `fake_engine` fixture.

```python
def test_replay_skips_venues_the_runner_does_not_trade(fake_engine: FakeEngine) -> None:
    runner = ExecutionRunner(
        fake_engine, OrderSpec("o-1", "buy", 1.0, 4, 4), "BTC/USD", logging.getLogger("t")
    )
    records = [(1, coinbase_snapshot(0), "coinbase"), (2, json.dumps(SNAPSHOT), "kraken")]
    run_replay(runner, iter(records))
    assert [book.venue for book in fake_engine.books] == ["kraken"]


def test_skipped_records_still_count_for_timestamp_order(fake_engine: FakeEngine) -> None:
    runner = ExecutionRunner(
        fake_engine, OrderSpec("o-1", "buy", 1.0, 4, 4), "BTC/USD", logging.getLogger("t")
    )
    records = [(5, coinbase_snapshot(0), "coinbase"), (2, json.dumps(SNAPSHOT), "kraken")]
    with pytest.raises(ReplayError, match="non-decreasing"):
        run_replay(runner, iter(records))
```

- [ ] **Step 2: Run the tests and confirm they fail.** Run `cd python && pytest tests/test_cli.py tests/test_replay.py -q`. Expected: `--venues` is an unrecognized argument, and replay raises `ValueError: unconfigured venue`.

- [ ] **Step 3: Implement.**

In `python/slipstream/replay.py` `run_replay`, skip records for venues the runner does not trade, after the monotonic check:

```python
        last_ns = recv_ns
        if venue not in runner.venues:
            continue
        runner.on_message(raw, recv_ns, venue)
```

In `python/slipstream/cli.py`:
- Import `cast` from `typing`.
- Add `VENUES` and `Venue` to the `slipstream.models` import.
- Add the parser:

```python
def _venues(value: str) -> tuple[Venue, ...]:
    names = [name.strip() for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in VENUES]
    if not names or unknown:
        raise argparse.ArgumentTypeError(f"venues must be a comma list of {', '.join(VENUES)}")
    return tuple(cast(Venue, name) for name in dict.fromkeys(names))
```

In `build_parser`, after the `for sub in (live, replay):` block, add:

```python
    for sub in (live, replay, compare, record):
        sub.add_argument(
            "--venues",
            type=_venues,
            default=("kraken",),
            help="comma list of venues; must match the engine's --venue flags",
        )
```

Update the parser description to "Paper-trade execution schedules across Kraken and Coinbase public order books (live or recorded).". Update the `live` and `record` help strings from "Kraken" to "public".

In `_record`, pass the venues through:

```python
            count = asyncio.run(
                record_stream(handle, args.symbol, args.depth, args.duration, venues=args.venues)
            )
```

In `main`, replace the runner construction and live call:

```python
        client.wait_ready()
        fees = client.venue_fees()
        if set(fees) != set(args.venues):
            log.error(
                f"engine venues {sorted(fees)} do not match --venues {sorted(args.venues)}; "
                "start the engine with one --venue flag per venue"
            )
            return 1
        runner = ExecutionRunner(
            client,
            specs,
            args.symbol,
            log,
            calibration,
            venues=args.venues,
            book_depth=getattr(args, "depth", 10),
            fee_bps=fees,
        )
        replay_file = getattr(args, "file", None)
        if replay_file is not None:
            run_replay(runner, read_replay(replay_file))
        else:
            deadline_s = args.duration + _DEADLINE_GRACE_S
            asyncio.run(
                run_live(runner, args.symbol, args.depth, deadline_s=deadline_s, venues=args.venues)
            )
```

`return 1` inside `try` still runs `finally: client.close()`.

- [ ] **Step 4: Run the tests and confirm they pass.** Run `cd python && pytest -q && mypy slipstream && ruff check . && ruff format --check .`. Expected: everything passes.

- [ ] **Step 5: Commit.**

```bash
git add python/slipstream/cli.py python/slipstream/replay.py python/tests/test_cli.py python/tests/test_replay.py
git commit -m "feat(cli): add --venues and refuse to run when it differs from the engine's venues"
```

- [ ] **Step 6: Review.** Run `/security-review` and `/caveman:caveman-review` on this task's diff. Fix valid findings test-first.

---

### Task 6: Report fees, routed all-in cost, per-venue cost, and routing gain

**Files:**
- Modify: `python/slipstream/cli.py`
- Test: `python/tests/test_cli.py`

- [ ] **Step 1: Write the failing tests.** Append to `python/tests/test_cli.py`, and import `routing_gain_bps` from `slipstream.cli`:

```python
def two_venue_status(**overrides: object) -> pb.OrderStatus:
    status = pb.OrderStatus(
        order_id="o-1",
        algo="twap",
        state=pb.ORDER_STATE_COMPLETED,
        total_qty=0.05,
        filled_qty=0.05,
        avg_fill_price=100019.0,
        arrival_mid=100002.5,
        slippage_bps=1.65,
        immediate_cost_bps=1.65,
        fees_paid=0.100015,
        fees_bps=0.2,
        routed_all_in_bps=1.85,
        venue_costs=[
            pb.VenueCost(venue="kraken", all_in_bps=1.95, available=True),
            pb.VenueCost(venue="coinbase", all_in_bps=3.05, available=True),
        ],
    )
    for key, value in overrides.items():
        setattr(status, key, value)
    return status


def test_routing_gain_is_best_available_venue_minus_routed() -> None:
    assert routing_gain_bps(two_venue_status()) == pytest.approx(0.10)


def test_routing_gain_ignores_unavailable_venues_and_is_none_without_any() -> None:
    status = two_venue_status()
    status.venue_costs[0].available = False
    assert routing_gain_bps(status) == pytest.approx(1.20)
    status.venue_costs[1].available = False
    assert routing_gain_bps(status) is None


def test_summary_shows_fees_all_in_venues_and_gain() -> None:
    text = format_summary(two_venue_status())
    assert "fees         0.20 bps (0.10 paid)" in text
    assert "all-in       1.85 bps" in text
    assert "  kraken     1.95 bps" in text
    assert "  coinbase   3.05 bps" in text
    assert "routing gain 0.10 bps" in text


def test_summary_marks_unavailable_venue_na() -> None:
    status = two_venue_status()
    status.venue_costs[1].available = False
    assert "  coinbase   n/a" in format_summary(status)


def test_single_venue_summary_has_no_venue_breakdown() -> None:
    status = pb.OrderStatus(
        order_id="o",
        venue_costs=[pb.VenueCost(venue="kraken", all_in_bps=1.0, available=True)],
    )
    text = format_summary(status)
    assert "fees" in text
    assert "routing gain" not in text
    assert "  kraken" not in text



def test_comparison_table_adds_venue_columns_for_two_venues() -> None:
    header, row = format_comparison([two_venue_status()], []).splitlines()
    assert header.split()[-11:] == [
        "fee", "bps", "all-in", "bps", "kraken", "bps", "coinbase", "bps", "gain", "bps", "fills",
    ]
    assert row.split()[-6:] == ["0.20", "1.85", "1.95", "3.05", "0.10", "0"]


def test_comparison_table_marks_unavailable_venue_na() -> None:
    status = two_venue_status()
    status.venue_costs[1].available = False
    row = format_comparison([status], []).splitlines()[1]
    assert row.split()[-4:] == ["1.95", "n/a", "0.10", "0"]
```

Update the existing `test_format_comparison_table` expectations for the single-venue layout: its header gains `fee bps` and `all-in bps` before `fills`, and each row gains `0.00 0.00` before the fill count:

```python
    assert lines[0].split() == [
        "algo", "state", "filled", "avg", "px", "slip", "bps", "1-shot", "bps",
        "saved", "bps", "fee", "bps", "all-in", "bps", "fills",
    ]
    assert lines[1].split() == [
        "twap", "COMPLETED", "1", "101.00", "100.00", "150.00", "50.00", "0.00", "0.00", "2",
    ]
    assert lines[2].split() == [
        "pov", "HALTED", "0.5", "101.00", "100.00", "150.00", "50.00", "0.00", "0.00", "1",
    ]
```

(`ruff format` will reflow these lists; that is fine.)

- [ ] **Step 2: Run the tests and confirm they fail.** Run `cd python && pytest tests/test_cli.py -q`. Expected: `ImportError` for `routing_gain_bps`.

- [ ] **Step 3: Implement.** In `python/slipstream/cli.py`, add the helpers above `format_summary`:

```python
def routing_gain_bps(status: pb.OrderStatus) -> float | None:
    available = [cost.all_in_bps for cost in status.venue_costs if cost.available]
    if not available:
        return None
    return min(available) - status.routed_all_in_bps


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _bps(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f} bps"
```

In `format_summary`, insert these lines just before the `halt_reason` check:

```python
    lines += [
        f"fees         {status.fees_bps:.2f} bps ({status.fees_paid:.2f} paid)",
        f"all-in       {status.routed_all_in_bps:.2f} bps (slippage + fees, routed)",
    ]
    if len(status.venue_costs) > 1:
        for cost in status.venue_costs:
            alone = cost.all_in_bps if cost.available else None
            lines.append(f"{'  ' + cost.venue:<13}{_bps(alone)} (all-in on this venue alone)")
        lines.append(f"routing gain {_bps(routing_gain_bps(status))} (vs best single venue)")
```

Replace `format_comparison`:

```python
def format_comparison(statuses: Sequence[pb.OrderStatus], fills: Sequence[Fill]) -> str:
    venues = [cost.venue for cost in statuses[0].venue_costs] if statuses else []
    multi = len(venues) > 1
    header = (
        f"{'algo':<16}{'state':<11}{'filled':>10}{'avg px':>12}{'slip bps':>10}"
        f"{'1-shot bps':>12}{'saved bps':>11}{'fee bps':>9}{'all-in bps':>12}"
    )
    if multi:
        header += "".join(f"{venue + ' bps':>14}" for venue in venues) + f"{'gain bps':>10}"
    rows = [header + f"{'fills':>7}"]
    for status in statuses:
        state = pb.OrderState.Name(status.state).removeprefix("ORDER_STATE_")
        count = sum(1 for fill in fills if fill.order_id == status.order_id)
        saved = status.immediate_cost_bps - status.slippage_bps
        row = (
            f"{status.algo:<16}{state:<11}{status.filled_qty:>10.8g}"
            f"{status.avg_fill_price:>12.2f}{status.slippage_bps:>10.2f}"
            f"{status.immediate_cost_bps:>12.2f}{saved:>11.2f}"
            f"{status.fees_bps:>9.2f}{status.routed_all_in_bps:>12.2f}"
        )
        if multi:
            row += "".join(
                f"{_num(cost.all_in_bps if cost.available else None):>14}"
                for cost in status.venue_costs
            )
            row += f"{_num(routing_gain_bps(status)):>10}"
        rows.append(row + f"{count:>7}")
    return "\n".join(rows)
```

- [ ] **Step 4: Run the tests and confirm they pass.** Run `cd python && pytest tests/test_cli.py -q && ruff format . && ruff check . && mypy slipstream`. Expected: everything passes.

- [ ] **Step 5: Commit.**

```bash
git add python/slipstream/cli.py python/tests/test_cli.py
git commit -m "feat(cli): report fees, routed all-in cost, per-venue cost, and routing gain"
```

- [ ] **Step 6: Review.** Run `/security-review` and `/caveman:caveman-review` on this task's diff. Fix valid findings test-first.

---

### Task 7: Two-venue end-to-end test through the real engine

**Files:**
- Create: `python/tests/fixtures/two_venue_btcusd_replay.jsonl`
- Create: `python/tests/test_integration_routing.py`
- Modify: `python/tests/conftest.py`

**Hand derivation.** The engine runs with `--venue kraken:fee_bps=0 --venue coinbase:fee_bps=1`. The order is a TWAP buy of 0.05 in 1 slice, so one child of 0.05 fills at submit time.

| Venue | Bids | Asks (gross) | Effective ask = ask × (1 + fee) |
|---|---|---|---|
| kraken (0 bps) | 99990 × 0.5 | 100010 × 0.02, 100030 × 1.0 | 100010, 100030 |
| coinbase (1 bps) | 99995 × 0.5 | 100015 × 0.01, 100025 × 1.0 | 100025.0015, 100035.0025 |

- **Consolidated mid** is (99995 + 100010) / 2 = **100002.5**.
- **Walk**, best effective first:
  1. kraken 0.02 @ 100010.
  2. coinbase 0.01 @ 100015 (effective 100025.0015).
  3. For the last 0.02, **kraken's 100030 (effective 100030) beats coinbase's cheaper gross 100025 (effective 100035.0025)**, so the fee flips the choice.
- **Legs** (sorted by venue registration order):
  - kraken 0.04 @ avg 100020.0, fee 0.
  - coinbase 0.01 @ 100015.0, fee 1000.15 × 0.0001 = 0.100015.
- **Totals:**
  - Gross notional 5000.95, avg 100019.0.
  - Slippage = 16.5 / 100002.5 × 1e4 = **1.64996 bps**.
  - Fees = 0.100015, or **0.19999 bps** of notional.
  - Routed all-in avg = 5001.050015 / 0.05 = 100021.0003, which is **1.84998 bps**.
- **Kraken alone:** 0.02 @ 100010 + 0.03 @ 100030 = 5001.1, avg 100022, which is **1.94995 bps**.
- **Coinbase alone:** 0.01 @ 100015 + 0.04 @ 100025 = 5001.15, plus fee 0.500115, giving avg 100033.0023, which is **3.05015 bps**.
- **Routing gain** = 1.94995 − 1.84998 = **0.09997 bps**.
- **One-shot** (gross, same walk at submit) = **1.64996 bps**.

- [ ] **Step 1: Create the fixture** `python/tests/fixtures/two_venue_btcusd_replay.jsonl`. Each record is one line:

```
{"recv_ns": 1700000000000000000, "venue": "kraken", "msg": {"method": "subscribe", "result": {"channel": "book", "depth": 10, "snapshot": true, "symbol": "BTC/USD"}, "success": true, "time_in": "2023-11-14T22:13:20.000000Z", "time_out": "2023-11-14T22:13:20.000100Z"}}
{"recv_ns": 1700000000000000000, "venue": "kraken", "msg": {"channel": "book", "type": "snapshot", "data": [{"symbol": "BTC/USD", "bids": [{"price": 99990.0, "qty": 0.5}], "asks": [{"price": 100010.0, "qty": 0.02}, {"price": 100030.0, "qty": 1.0}], "checksum": 0}]}}
{"recv_ns": 1700000000000000000, "venue": "coinbase", "msg": {"channel": "l2_data", "client_id": "", "timestamp": "2023-11-14T22:13:20.000000Z", "sequence_num": 0, "events": [{"type": "snapshot", "product_id": "BTC-USD", "updates": [{"side": "bid", "event_time": "2023-11-14T22:13:20.000000Z", "price_level": "99995", "new_quantity": "0.5"}, {"side": "offer", "event_time": "2023-11-14T22:13:20.000000Z", "price_level": "100015", "new_quantity": "0.01"}, {"side": "offer", "event_time": "2023-11-14T22:13:20.000000Z", "price_level": "100025", "new_quantity": "1.0"}]}]}}
{"recv_ns": 1700000001000000000, "venue": "kraken", "msg": {"channel": "heartbeat"}}
```

- [ ] **Step 2: Add a two-venue engine fixture.** In `python/tests/conftest.py`, refactor `engine_address` into a helper plus two fixtures:

```python
def _run_engine(*extra: str) -> Iterator[str]:
    if not ENGINE_BIN.exists():
        pytest.fail(f"engine binary not found at {ENGINE_BIN}; run scripts/build_engine.sh")
    proc = subprocess.Popen(
        [
            str(ENGINE_BIN),
            "--listen",
            "127.0.0.1:0",
            "--symbol",
            "BTC/USD",
            "--max-order-notional",
            "10000",
            "--max-position",
            "1.0",
            *extra,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        match = re.search(r"port (\d+)", line)
        if match is None:
            pytest.fail(f"engine did not start: {line!r}")
        yield f"127.0.0.1:{match.group(1)}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def engine_address() -> Iterator[str]:
    yield from _run_engine()


@pytest.fixture
def two_venue_engine_address() -> Iterator[str]:
    yield from _run_engine("--venue", "kraken:fee_bps=0", "--venue", "coinbase:fee_bps=1")
```

- [ ] **Step 3: Write the test.** Create `python/tests/test_integration_routing.py`:

```python
import logging
from pathlib import Path

import pytest

from slipstream.cli import routing_gain_bps
from slipstream.engine_client import EngineClient
from slipstream.models import OrderSpec
from slipstream.replay import read_replay, run_replay
from slipstream.runner import ExecutionRunner
from slipstream.v1 import execution_pb2 as pb

FIXTURE = Path(__file__).parent / "fixtures" / "two_venue_btcusd_replay.jsonl"


def test_routes_across_two_venues_by_all_in_price(two_venue_engine_address: str) -> None:
    client = EngineClient(two_venue_engine_address)
    try:
        client.wait_ready()
        fees = client.venue_fees()
        assert fees == {"kraken": 0.0, "coinbase": 1.0}
        runner = ExecutionRunner(
            client,
            OrderSpec("route-1", "buy", 0.05, 6, 1),
            "BTC/USD",
            logging.getLogger("test"),
            venues=("kraken", "coinbase"),
            fee_bps=fees,
        )
        run_replay(runner, read_replay(FIXTURE))

        legs = [(f.venue, f.qty, f.price, f.fee) for f in runner.fills]
        assert legs == [
            ("kraken", pytest.approx(0.04), pytest.approx(100020.0), pytest.approx(0.0)),
            ("coinbase", pytest.approx(0.01), pytest.approx(100015.0), pytest.approx(0.100015)),
        ]
        status = runner.order_status()
        assert status is not None
        assert status.state == pb.ORDER_STATE_COMPLETED
        assert status.arrival_mid == pytest.approx(100002.5)
        assert status.avg_fill_price == pytest.approx(100019.0)
        assert status.slippage_bps == pytest.approx(1.64996, abs=1e-4)
        assert status.fees_paid == pytest.approx(0.100015)
        assert status.fees_bps == pytest.approx(0.19999, abs=1e-4)
        assert status.routed_all_in_bps == pytest.approx(1.84998, abs=1e-4)
        costs = {c.venue: (c.available, c.all_in_bps) for c in status.venue_costs}
        assert costs["kraken"] == (True, pytest.approx(1.94995, abs=1e-4))
        assert costs["coinbase"] == (True, pytest.approx(3.05015, abs=1e-4))
        assert routing_gain_bps(status) == pytest.approx(0.09997, abs=1e-4)
    finally:
        client.close()


def test_kraken_only_replay_of_two_venue_file_uses_kraken_alone(engine_address: str) -> None:
    client = EngineClient(engine_address)
    try:
        client.wait_ready()
        runner = ExecutionRunner(
            client, OrderSpec("solo-1", "buy", 0.05, 6, 1), "BTC/USD", logging.getLogger("test")
        )
        run_replay(runner, read_replay(FIXTURE))
        assert {f.venue for f in runner.fills} == {"kraken"}
        status = runner.order_status()
        assert status is not None
        assert status.avg_fill_price == pytest.approx(100022.0)
    finally:
        client.close()
```

- [ ] **Step 4: Run the tests and confirm they pass.** Run `bash scripts/build_engine.sh && cd python && pytest tests/test_integration_routing.py -v`. Expected: 2 passed.
  - If a number differs, **stop and re-derive it by hand from the engine code** (`engine/src/router.cpp`, `engine/src/engine.cpp`). Never loosen a tolerance to make it pass.
  - Report the discrepancy in your status.

- [ ] **Step 5: Run the full CI.** Run `bash scripts/ci.sh`. Expected: `CI OK`.

- [ ] **Step 6: Commit.**

```bash
git add python/tests/conftest.py python/tests/fixtures/two_venue_btcusd_replay.jsonl python/tests/test_integration_routing.py
git commit -m "test: route a two-venue replay through the real engine with hand-derived legs"
```

- [ ] **Step 7: Review.** Run `/security-review` and `/caveman:caveman-review` on this task's diff. Fix valid findings test-first.

---

### Task 8: Demo scripts, docs, and the live two-venue comparison

**Files:**
- Modify: `scripts/demo_live.sh`, `scripts/demo_compare.sh`, `README.md`, `note.md`

- [ ] **Step 1: Update the demo scripts.** Replace the body of `scripts/demo_live.sh` after `export PYTHONPATH=...`:

```bash
# Illustrative entry-tier taker fees in bps. Set your own tier:
#   KRAKEN_FEE_BPS=26 COINBASE_FEE_BPS=40 bash scripts/demo_live.sh ...
build/engine/slipstream_engine --listen 127.0.0.1:50051 \
  --max-order-notional 2000 --max-position 0.05 \
  --venue "kraken:fee_bps=${KRAKEN_FEE_BPS:-40}" \
  --venue "coinbase:fee_bps=${COINBASE_FEE_BPS:-60}" &
ENGINE_PID=$!
trap 'kill "$ENGINE_PID" 2>/dev/null || true' EXIT
python -m slipstream.cli live --engine 127.0.0.1:50051 --venues kraken,coinbase "$@"
```

Update the header comment to: "Paper-trade one order routed across Kraken and Coinbase public BTC/USD books. No keys, no real orders."

Apply the same change to `scripts/demo_compare.sh`: keep its `--max-position 0.1`, use the `compare` subcommand, and use the header comment "every algorithm side by side".

The fee values come from trusted env vars; the engine validates them and refuses to start on bad input.

- [ ] **Step 2: Smoke-test the scripts offline.**
  - Start the engine exactly as the script does. Run `python -m slipstream.cli replay --file python/tests/fixtures/two_venue_btcusd_replay.jsonl --venues kraken,coinbase --side buy --qty 0.01 --duration 6 --slices 1 --engine 127.0.0.1:50051`.
  - Expected: a summary with `fees`, `all-in`, two venue lines, and `routing gain`.
  - Then run the same command without `--venues`. Expected: exit 1 with "do not match".

- [ ] **Step 3: Run a live two-venue comparison** (public data, paper only).
  - Run `bash scripts/demo_compare.sh --side buy --qty 0.01 --duration 120 --slices 12`.
  - Save the printed table and the date and time (UTC) to the scratchpad.
  - If a feed fails, for example a network or Coinbase change, record the exact error, stop, and report. Do not work around it.

- [ ] **Step 4: Update the README.**
  - Add a "Smart routing across venues" section after "Compare algorithms", covering:
    - What the router does: each child is split across venues by fee-adjusted price, and each venue alone is priced counterfactually on the same books.
    - How to run: the `--venue` engine flags and the matching `--venues` CLI flag.
    - That fees are the user's own tier (illustrative defaults 40 and 60 bps).
    - The live table from Step 3 with an honest reading. At entry-tier fees a 20 bps fee gap usually dominates, so most small orders route entirely to the cheaper-fee venue and the routing gain is small. Say so plainly.
  - Remove "Second venue and smart routing across venues" from the Roadmap.
  - Update "How it works" to mention Coinbase.

- [ ] **Step 5: Update `note.md`.**
  - Add a dev-log entry: PRs #11/#12/#13; the Coinbase book-erosion bug and fix; engine-as-source-of-truth for fees; the live result.
  - Add a decision row: "Fees configured once on the engine; Python reads them from GetStatus — avoids two configs drifting."

- [ ] **Step 6: Run the full CI and commit.** Run `bash scripts/ci.sh`. Expected: `CI OK`.

```bash
git add scripts/demo_live.sh scripts/demo_compare.sh README.md note.md
git commit -m "docs: document smart routing across Kraken and Coinbase with a live comparison"
```

- [ ] **Step 7: Review.** Run `/security-review` and `/caveman:caveman-review` on this task's diff. Fix valid findings test-first.

---

## Finish

- [ ] Run a final whole-branch `/security-review` and `/caveman:caveman-review` on `origin/main...HEAD`.
- [ ] Run `bash scripts/ci.sh` and confirm `CI OK` (superpowers:verification-before-completion).
- [ ] Confirm every commit author is the noreply address: `git log --format='%ae' origin/main..HEAD | sort -u`.
- [ ] Push, open PR 3 "feat: smart routing across Kraken and Coinbase, end to end", and merge when CI is green (standing approval).
