"""The contract every registered venue adapter must meet. A new venue gets these tests for free
by adding tests/fixtures/venues/<name>.jsonl (raw WebSocket messages, one per line, as recorded
from a BTC/USD subscription) and <name>_rules.json (a recorded trading-rules response)."""

import json
import math
import re
from pathlib import Path

import pytest

from slipstream.models import BookUpdate, MarketDataError, TradeBatch
from slipstream.venue_rules import VenueRulesError
from slipstream.venues.base import Parsed, VenueAdapter
from slipstream.venues.registry import ADAPTERS

FIXTURES = Path(__file__).parent / "fixtures" / "venues"
SYMBOL = "BTC/USD"
DEPTH = 10

pytestmark = pytest.mark.parametrize("venue", ADAPTERS, ids=[a.name for a in ADAPTERS])


def _recorded(venue: VenueAdapter) -> list[str]:
    return (FIXTURES / f"{venue.name}.jsonl").read_text(encoding="utf-8").splitlines()


def _parse_all(venue: VenueAdapter, replay: bool = False) -> list[Parsed]:
    new = venue.replay_parser if replay else venue.new_parser
    parse = new(SYMBOL, DEPTH)
    return [parse(line) for line in _recorded(venue)]


def test_name_url_and_fee_are_well_formed(venue: VenueAdapter) -> None:
    assert re.fullmatch(r"[a-z][a-z0-9]{0,15}", venue.name)
    assert venue.ws_url.startswith("wss://")
    assert 0 < venue.max_message_bytes <= 64 << 20
    assert 0 <= venue.entry_taker_fee_bps <= 1000
    assert all(issubclass(error, MarketDataError) for error in venue.resync_errors)


def test_subscriptions_are_json_objects(venue: VenueAdapter) -> None:
    messages = venue.subscriptions(SYMBOL, DEPTH)
    assert messages
    assert all(isinstance(json.loads(message), dict) for message in messages)


def test_recorded_feed_yields_snapshots_and_live_trades_for_the_common_symbol(
    venue: VenueAdapter,
) -> None:
    updates = [u for u in _parse_all(venue) if isinstance(u, (BookUpdate, TradeBatch))]
    assert any(isinstance(u, BookUpdate) and u.is_snapshot and u.bids and u.asks for u in updates)
    assert any(isinstance(u, TradeBatch) and not u.is_snapshot for u in updates)
    assert {(u.symbol, u.venue) for u in updates} == {(SYMBOL, venue.name)}


def test_recorded_books_are_sane(venue: VenueAdapter) -> None:
    for update in _parse_all(venue):
        if not isinstance(update, BookUpdate):
            continue
        for price, qty in (*update.bids, *update.asks):
            assert math.isfinite(price) and price > 0
            assert math.isfinite(qty) and qty >= 0
        if update.is_snapshot and update.bids and update.asks:
            assert update.bids[0][0] < update.asks[0][0]


def test_replay_parser_reads_the_recorded_feed(venue: VenueAdapter) -> None:
    updates = [u for u in _parse_all(venue, replay=True) if isinstance(u, BookUpdate)]
    assert any(u.is_snapshot for u in updates)


@pytest.mark.parametrize("raw", ["not json", b"\xff\xfe", "[1, 2]", "{"])
def test_malformed_input_raises_market_data_error(venue: VenueAdapter, raw: str | bytes) -> None:
    with pytest.raises(MarketDataError):
        venue.new_parser(SYMBOL, DEPTH)(raw)


def test_oversized_input_raises_market_data_error(venue: VenueAdapter) -> None:
    with pytest.raises(MarketDataError):
        venue.new_parser(SYMBOL, DEPTH)(" " * (venue.max_message_bytes + 1))


def test_recorded_rules_parse_to_finite_non_negative_values(venue: VenueAdapter) -> None:
    urls: list[str] = []

    def fetch(url: str) -> bytes:
        urls.append(url)
        return (FIXTURES / f"{venue.name}_rules.json").read_bytes()

    rules = venue.fetch_rules(SYMBOL, fetch)
    assert len(urls) == 1 and urls[0].startswith("https://")
    for value in (rules.min_qty, rules.qty_step, rules.min_notional):
        assert math.isfinite(value) and value >= 0


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b"{}"])
def test_malformed_rules_raise_venue_rules_error(venue: VenueAdapter, body: bytes) -> None:
    with pytest.raises(VenueRulesError):
        venue.fetch_rules(SYMBOL, lambda _url: body)


def test_unsupported_symbol_rules_raise_venue_rules_error(venue: VenueAdapter) -> None:
    with pytest.raises(VenueRulesError):
        venue.fetch_rules("ZZZ/QQQ", lambda _url: b"{}")
