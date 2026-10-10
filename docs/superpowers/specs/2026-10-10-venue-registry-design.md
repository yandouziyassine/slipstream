# Venue Registry — Design

Date: 2026-10-10
Status: Proposed
Scope: make adding an exchange a one-module change, then rank the exchanges to add next. Paper trading only; public market data only.

## 1. Purpose

Slipstream streams two venues, Kraken and Coinbase. Each one is wired by hand through about a dozen places, so a third venue would touch the feed, recorder, replay, session, CLI, rules fetcher and engine. The user wants "as much exchange information as possible to have good data". Before adding many venues in parallel, every per-venue fact moves behind one small interface, `VenueAdapter`, listed in one registry. A new venue is then one new module plus one registry line, built and tested by its own agent without touching shared plumbing.

The refactor changes no behaviour. Every existing test passes unchanged except `config_test.cpp`, which asserted the old allowlist. Replays give identical results, and `venue-flags` prints identical output.

## 2. Where venues were hard-coded

| Place | What was venue-specific |
|---|---|
| `models.py` | `Venue = Literal["kraken", "coinbase"]`, `VENUES` |
| `venue_ws.py` | WS URLs, max message size, subscribe messages, parser choice, `BookChecksumError` treated as reconnectable |
| `feed_process.py`, `recorder.py` | the `venue_ws` dispatch above, venue allowlist |
| `replay.py` | Kraken-only checksum re-check of recorded books (`_KrakenBookCheck`) |
| `session.py` | replay parsers (`parse_message` for Kraken, `CoinbaseStream` for Coinbase), reset of the Coinbase parser at a reconnect marker |
| `venue_rules.py` | REST URLs, symbol maps, `if venue == ...` dispatch |
| `cli.py`, `engine_stream.py`, `engine_process.py` | allowlist (`VENUES`, regex `kraken\|coinbase`) |
| `engine/src/config.*` | `kKnownVenues{"kraken", "coinbase"}` |
| `site/render.py` (not in scope) | a fixed "Kraken / Coinbase" fill-split column |

What actually differs per venue:

| Concern | Kraken | Coinbase |
|---|---|---|
| WS URL | `wss://ws.kraken.com/v2` | `wss://advanced-trade-ws.coinbase.com` |
| Subscribe | `book` (depth 10/25/100/500/1000) + `trade` | `level2` + `market_trades` + `heartbeats` |
| Book semantics | snapshot, then deltas within the subscribed depth, CRC32 checksum on every message | full-depth snapshot, then full-depth deltas, per-connection `sequence_num` |
| Parser state | checksum mirror at the subscribed depth (`KrakenBook`) | full-book mirror; every emitted update is a top-N snapshot |
| Recovery | checksum mismatch: reconnect for a fresh snapshot | sequence gap: fail safe (stop) |
| Symbol | `BTC/USD` on the wire; REST pair `XBTUSD` / `XXBTZUSD` | `BTC-USD` |
| Rules REST | `GET /0/public/AssetPairs?pair=XBTUSD` | `GET /api/v3/brokerage/market/products/BTC-USD` |
| Replay | checksums re-checked from the recorded subscribe ack on; session parses statelessly | session re-creates the stateful parser at each reconnect marker |

## 3. Interface

`slipstream/venues/base.py`:

```python
Parsed = BookUpdate | TradeBatch | Reply | None   # None = heartbeat / nothing to send

@dataclass(frozen=True)
class Reply:                     # a message the venue requires us to send back
    text: str                    # e.g. Crypto.com's public/respond-heartbeat

Parser = Callable[[str | bytes], Parsed]

class RecordedCheck(Protocol):   # optional extra check of a recorded file at replay
    def check(self, msg: Any) -> None: ...   # msg decoded with Decimal floats
    def reset(self) -> None: ...             # at a reconnect marker

@dataclass(frozen=True)
class VenueAdapter:
    name: str                                    # [a-z][a-z0-9]{0,15}; engine/CLI/recording name
    ws_url: str                                  # wss:// only
    max_message_bytes: int
    entry_taker_fee_bps: float                   # public fee page, lowest tier (reference only)
    subscriptions: Callable[[str, int], list[str]]        # (symbol, depth) -> messages to send
    new_parser: Callable[[str, int], Parser]              # fresh per connection
    fetch_rules: Callable[[str, Fetch], VenueRules]       # (symbol, https GET) -> rules
    new_replay_parser: Callable[[str, int], Parser] | None = None   # default: new_parser
    new_recorded_check: Callable[[], RecordedCheck] | None = None
    resync_errors: tuple[type[MarketDataError], ...] = ()  # recovered by a fresh connection
```

Rules for every adapter:

- **Symbols.** Callers always pass the common symbol (`BTC/USD`). Each adapter maps it to its own wire and REST symbols. It raises `MarketDataError` (or `VenueRulesError`) for a symbol it does not support. Every `BookUpdate`/`TradeBatch` it yields carries the common symbol and `venue=name`.
- **Book semantics.** A `BookUpdate` with `is_snapshot=True` replaces the engine's book for that venue; otherwise it upserts levels (qty 0 deletes). A venue that only sends full-depth deltas must keep the whole book itself and emit top-N snapshots (as Coinbase does), because the engine keeps only N levels.
- **Fail safe.** Malformed, oversized, out-of-sequence or failed-checksum data raises a `MarketDataError` subclass. Only errors listed in `resync_errors` are retried with a fresh connection; any other stops the feed.
- **Replies.** A parser returns `Reply(text)` when the venue requires an answer. The feed and the recorder send it back on the same connection and treat it as a heartbeat. Replay ignores it. A reply is built only from fields the parser has validated (e.g. an integer id), never by echoing raw venue text.
- **Trades.** Historical trades sent at subscribe time are `TradeBatch(is_snapshot=True)` or dropped; they never count as live volume.

`slipstream/venues/registry.py` holds `ADAPTERS` (an explicit, ordered tuple), `VENUES` (their names) and `adapter(name)`. Importing the registry validates the names (pattern above, no duplicates). `models.Venue` becomes `str`. Every former allowlist (`cli`, `feed_process`, `session`, `replay`, `engine_stream`, `engine_process`) reads `VENUES`, and the session builds its replay parsers with `adapter(venue).replay_parser(...)`. This lands in two steps because of file ownership during parallel work: step 1 (this branch) routes the venue plumbing through the registry and keeps a copy of the names in `models.VENUES`, which a test holds equal to the registry. Step 2 moves `session`, `engine_stream` and `engine_process` onto the registry and deletes the copy and its test. Until step 2 lands, a new venue streams and records but does not yet replay.

Adding a venue = `slipstream/venues/<name>.py` (parser, rules parser, adapter), `tests/venues/test_<name>.py`, `tests/fixtures/venues/<name>.jsonl` (raw recorded WS messages, one per line) and `<name>_rules.json` (a recorded rules response), plus one line in `registry.py`. The shared contract suite `tests/test_venue_contract.py` runs over every registered adapter. It checks that the fixture parses into at least one snapshot and one trade batch with the common symbol and the adapter's name, that malformed and oversized input raises `MarketDataError`, that the recorded rules parse to finite non-negative rules, and that the URL and name are well formed.

### 3.1 Engine

Venue names become free-form but validated: `[a-z][a-z0-9]{0,15}`, at most one `--venue` per name. The Python registry stays the allowlist. `engine_process.validate_venue_flag` and `EngineChannel.venue_fees` only accept registered names, so a new venue needs no C++ change.

## 4. Venues to add next (BTC/USD, USD-quoted, free public data, no key)

Checked on 2026-10-10 against official docs and live public REST responses. Fees are the lowest-tier taker fee on each venue's public fee page. They are the user's own tier, so they stay explicit `--fees` input.

| Rank | Venue | WS (public, no key) | Book format | Integrity | Depth | Rules REST | Entry taker | Notes |
|---|---|---|---|---|---|---|---|---|
| 1 | **Bitstamp** | `wss://ws.bitstamp.net`; `{"event":"bts:subscribe","data":{"channel":"order_book_btcusd"}}` + `live_trades_btcusd` | `order_book_*`: every message is a full top-100 snapshot (stateless). `diff_order_book_*`: full-depth deltas, needs a REST snapshot sequenced by `microtimestamp` | no checksum or sequence on book channels; `bts:request_reconnect` before maintenance | 100 (snapshot channel) | `GET /api/v2/trading-pairs-info/` → `minimum_order "10.00 USD"`, `base_decimals 8` | 0.40% | 1024 subs/connection, client frames < 512 B. Simplest correct adapter: snapshot channel, like Coinbase's emitted view |
| 2 | **Gemini** | `wss://ws.gemini.com?snapshot=-1`; `{"id":1,"method":"subscribe","params":["btcusd@depth@100ms","btcusd@trade"]}` | first `depthUpdate` is the full snapshot, then deltas every 100 ms (qty 0 deletes) | `U`/`u` update IDs: a gap means discard and resubscribe | full | `GET https://api.gemini.com/v1/symbols/details/btcusd` → `min_order_size 0.00001`, `tick_size 1e-8` (qty step), `quote_increment 0.01` | 1.20% (API schedule, ≥ $0 tier) | Partial-depth `@depth20@100ms` snapshots also available |
| 3 | **Bitfinex** | `wss://api-pub.bitfinex.com/ws/2`; `{"event":"subscribe","channel":"book","symbol":"tBTCUSD","prec":"P0","len":"25"}` + `trades` | snapshot `[chan,[[price,count,amount],…]]`, then single-level updates; count 0 deletes; amount sign = side | CRC32 checksum over top 25 per side (`conf` flag 131072); `SEQ_ALL` 65536 (beta) | 1/25/100/250 | `GET https://api-pub.bitfinex.com/v2/conf/pub:info:pair` → min 0.00004, max 2000 | 0% (zero-fee schedule) | P0 aggregates prices to 5 significant figures (about $1 at $83k). `hb` every 15 s. Info events 20051/20060/20061 mean reconnect. 30 subs/connection; REST 90 req/min |
| 4 | **Crypto.com Exchange** | `wss://stream.crypto.com/exchange/v1/market`; `{"id":1,"method":"subscribe","params":{"channels":["book.BTC_USD.50"],"book_subscription_type":"SNAPSHOT_AND_UPDATE","book_update_frequency":100},"nonce":…}` + `trade.BTC_USD` | snapshot, then deltas | `u` / `pu` sequence (delta's `pu` must equal last `u`) | 10 or 50 | `GET https://api.crypto.com/exchange/v1/public/get-instruments` → `BTC_USD price_tick_size 0.01, qty_tick_size 0.00001` | not verified (fee page redirects away from the user's region) | Server heartbeat every 30 s needs `public/respond-heartbeat` within 5 s, which is why `Reply` exists. Trade channel replays the last 50 trades on subscribe (snapshot) |
| 5 | **Binance.US** | `wss://stream.binance.us:9443/ws/btcusd@depth20@100ms` + `btcusd@trade` | partial-depth stream: top-20 snapshot every 100 ms (stateless). Diff stream needs a REST `/api/v3/depth` snapshot + `U`/`u` | `lastUpdateId` / `U`/`u` | 5/10/20 (partial) | `GET https://api.binance.us/api/v3/exchangeInfo?symbol=BTCUSD` → `LOT_SIZE 0.00001/0.00001`, `MIN_NOTIONAL 1`, `tickSize 0.01` | about 0.02% (VIP 1 per fee page; verify) | 5 incoming msgs/s, 1024 streams/connection, connections last at most 24 h. BTCUSD is thinner than BTCUSDT there |
| 6 | Bitso | `wss://ws.bitso.com` (not researched in depth) | — | diff-orders has sequence numbers | — | `GET https://api.bitso.com/v3/available_books/` → `btc_usd` min amount 6e-7, min value 0.5, tick 1 | — | thin USD book; low priority |
| 7 | CEX.IO | (not researched in depth) | — | — | — | `POST https://trade.cex.io/api/spot/rest-public/get_pairs_info` → `BTC-USD baseMin 0.00019, quoteMin 10` | — | low priority |

Excluded:
- **OKX**: no `BTC-USD` spot instrument (`code 51001` on www/app/us hosts); only USDT/USDC quotes.
- **Bullish**: the API answers 403 ("not currently available in your location") from the user's region.
- **Paxos (itBit)**: its BTC order book WebSocket is the authenticated Smart Order Routing feed; the public feed is stablecoin-only.
- USDT-only venues (Bybit, KuCoin, HTX, Gate, Bitget) and EUR venues (Bitvavo, Bitpanda): wrong quote currency.

Fee facts that affect existing defaults: Kraken Pro Tier 1 (spot, $0+) is now 0.40% maker / **0.80% taker**. The scripts' illustrative `kraken=40` is the Tier 3 taker rate. Coinbase no longer publishes its Advanced fee table without sign-in, so the scripts' `coinbase=60` cannot be checked from public pages.

Sources: Bitstamp https://www.bitstamp.net/websocket/v2/ and https://www.bitstamp.net/fee-schedule/; Gemini https://developer.gemini.com/trading/websocket/introduction.md, https://developer.gemini.com/websocket/streams.md, https://developer.gemini.com/trading/rest-api/market-data/get-symbol-details.md, https://www.gemini.com/fees/api-fee-schedule; Bitfinex https://docs.bitfinex.com/reference/ws-public-books, https://docs.bitfinex.com/docs/ws-websocket-checksum, https://docs.bitfinex.com/docs/ws-general, https://docs.bitfinex.com/reference/rest-public-conf, https://www.bitfinex.com/fees/; Crypto.com https://exchange-developer.crypto.com/exchange/v1/docs/api/websocket/ws-channel-book-instrument-name-depth; Binance.US https://docs.binance.us/, https://www.binance.us/fees; Kraken https://www.kraken.com/features/fee-schedule; Paxos https://docs.paxos.com/llms.txt; OKX/Bullish/Bitso/CEX.IO: live public REST responses on 2026-10-10.

## 5. Testing

- Existing suites unchanged (C++ `config_test` updated for free-form names).
- `tests/test_venue_registry.py`: names valid and unique, `VENUES` order, `adapter()` lookup and unknown-name error, `fetch_venue_rules` dispatch.
- `tests/test_venue_contract.py`: parametrized over `ADAPTERS` (§3).
- `Reply` path: feed, recorder and replay session tested with a fake registered adapter.
