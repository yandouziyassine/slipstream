import inspect
import json
from typing import Any

import pytest

from slipstream import venue_rules
from slipstream.models import MarketDataError
from slipstream.venue_rules import (
    MAX_RESPONSE_BYTES,
    VenueRules,
    VenueRulesError,
    fetch_venue_rules,
    parse_coinbase_rules,
    parse_kraken_rules,
)


def kraken_body(
    pair: dict[str, Any] | None = None,
    errors: Any = None,
    result: Any = None,
    extra_pair: bool = False,
) -> bytes:
    entry: dict[str, Any] = {
        "altname": "XBTUSD",
        "ordermin": "0.00005",
        "costmin": "0.5",
        "lot_decimals": 8,
    }
    if pair is not None:
        entry.update(pair)
    body: dict[str, Any] = {"XXBTZUSD": entry} if result is None else result
    if extra_pair:
        body["XETHZUSD"] = entry
    return json.dumps({"error": [] if errors is None else errors, "result": body}).encode()


def coinbase_body(fields: dict[str, Any] | None = None) -> bytes:
    payload: dict[str, Any] = {
        "product_id": "BTC-USD",
        "status": "online",
        "trading_disabled": False,
        "base_min_size": "0.00000001",
        "base_increment": "0.00000001",
        "quote_min_size": "1",
    }
    if fields is not None:
        payload.update(fields)
    return json.dumps(payload).encode()


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def test_venue_rules_error_is_market_data_error() -> None:
    assert issubclass(VenueRulesError, MarketDataError)


# --- Kraken ---


def test_parses_kraken_rules() -> None:
    assert parse_kraken_rules(kraken_body()) == VenueRules(0.00005, 1e-08, 0.5)


def test_kraken_error_array_raises() -> None:
    with pytest.raises(VenueRulesError, match="Kraken error"):
        parse_kraken_rules(kraken_body(errors=["EQuery:Unknown asset pair"]))


def test_kraken_custom_pair_key() -> None:
    body = json.dumps(
        {
            "error": [],
            "result": {
                "XETHZUSD": {"ordermin": "0.01", "costmin": "1", "lot_decimals": 4},
            },
        }
    ).encode()
    assert parse_kraken_rules(body, pair_key="XETHZUSD") == VenueRules(0.01, 1e-04, 1.0)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[" * 100_000,
        json.dumps([1]).encode(),
        json.dumps({"error": [], "result": []}).encode(),
        kraken_body(extra_pair=True),
        kraken_body(result={}),
        kraken_body(result={"XXBTZUSD": "not-an-object"}),
        kraken_body(pair={"ordermin": None}),
        kraken_body(pair={"ordermin": 1.0}),
        kraken_body(pair={"ordermin": "nan"}),
        kraken_body(pair={"ordermin": "-1"}),
        kraken_body(pair={"ordermin": "1e400"}),
        kraken_body(pair={"ordermin": "1" * 65}),
        kraken_body(pair={"costmin": "nan"}),
        kraken_body(pair={"costmin": "-1"}),
        kraken_body(pair={"lot_decimals": True}),
        kraken_body(pair={"lot_decimals": "8"}),
        kraken_body(pair={"lot_decimals": -1}),
        kraken_body(pair={"lot_decimals": 13}),
        kraken_body(pair={"lot_decimals": 10**400}),
        b" " * (MAX_RESPONSE_BYTES + 10),
    ],
    ids=[
        "not-json",
        "deep",
        "not-object",
        "result-not-object",
        "two-pairs",
        "no-pair",
        "pair-not-object",
        "ordermin-none",
        "ordermin-number",
        "ordermin-nan",
        "ordermin-negative",
        "ordermin-overflow",
        "ordermin-long",
        "costmin-nan",
        "costmin-negative",
        "lot-decimals-bool",
        "lot-decimals-string",
        "lot-decimals-negative",
        "lot-decimals-too-large",
        "lot-decimals-huge-int",
        "oversized",
    ],
)
def test_hostile_kraken_raises(raw: bytes) -> None:
    with pytest.raises(VenueRulesError):
        parse_kraken_rules(raw)


# --- Coinbase ---


def test_parses_coinbase_rules() -> None:
    assert parse_coinbase_rules(coinbase_body()) == VenueRules(1e-08, 1e-08, 1.0)


def test_coinbase_product_mismatch_raises() -> None:
    with pytest.raises(VenueRulesError, match="product_id"):
        parse_coinbase_rules(coinbase_body({"product_id": "ETH-USD"}))


def test_coinbase_trading_disabled_raises() -> None:
    with pytest.raises(VenueRulesError, match="not trading"):
        parse_coinbase_rules(coinbase_body({"trading_disabled": True}))


def test_coinbase_status_not_online_raises() -> None:
    with pytest.raises(VenueRulesError, match="not trading"):
        parse_coinbase_rules(coinbase_body({"status": "offline"}))


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[" * 100_000,
        json.dumps([1]).encode(),
        coinbase_body({"trading_disabled": None}),
        coinbase_body({"trading_disabled": 0}),
        coinbase_body({"base_min_size": None}),
        coinbase_body({"base_min_size": 1.0}),
        coinbase_body({"base_min_size": "nan"}),
        coinbase_body({"base_min_size": "-1"}),
        coinbase_body({"base_min_size": "1e400"}),
        coinbase_body({"base_min_size": "1" * 65}),
        coinbase_body({"base_increment": "nan"}),
        coinbase_body({"quote_min_size": "-1"}),
        b" " * (MAX_RESPONSE_BYTES + 10),
    ],
    ids=[
        "not-json",
        "deep",
        "not-object",
        "trading-disabled-none",
        "trading-disabled-falsy-int",
        "base-min-size-none",
        "base-min-size-number",
        "base-min-size-nan",
        "base-min-size-negative",
        "base-min-size-overflow",
        "base-min-size-long",
        "base-increment-nan",
        "quote-min-size-negative",
        "oversized",
    ],
)
def test_hostile_coinbase_raises(raw: bytes) -> None:
    with pytest.raises(VenueRulesError):
        parse_coinbase_rules(raw)


# --- fetch_venue_rules ---


def test_fetch_venue_rules_calls_exact_urls() -> None:
    seen: list[str] = []

    def fake(url: str) -> bytes:
        seen.append(url)
        return kraken_body() if "kraken" in url else coinbase_body()

    rules = fetch_venue_rules(("kraken", "coinbase"), "BTC/USD", fetch=fake)
    assert seen == [
        "https://api.kraken.com/0/public/AssetPairs?pair=XBTUSD",
        "https://api.coinbase.com/api/v3/brokerage/market/products/BTC-USD",
    ]
    assert rules == {
        "kraken": VenueRules(0.00005, 1e-08, 0.5),
        "coinbase": VenueRules(1e-08, 1e-08, 1.0),
    }


def test_fetch_venue_rules_single_venue() -> None:
    rules = fetch_venue_rules(("coinbase",), "BTC/USD", fetch=lambda _url: coinbase_body())
    assert set(rules) == {"coinbase"}


def test_fetch_venue_rules_rejects_unsupported_symbol() -> None:
    with pytest.raises(VenueRulesError, match="mapping"):
        fetch_venue_rules(("kraken",), "DOGE/USD", fetch=lambda _url: b"")


def test_fetch_venue_rules_propagates_parse_errors() -> None:
    with pytest.raises(VenueRulesError):
        fetch_venue_rules(("kraken",), "BTC/USD", fetch=lambda _url: b"not json")


# --- _http_get ---


def test_http_get_default_opener_is_no_redirect_opener() -> None:
    assert inspect.signature(venue_rules._http_get).parameters["opener"].default == (
        venue_rules.NO_REDIRECT_OPENER.open
    )


def test_http_get_rejects_oversized_response() -> None:
    def opener(request: Any, timeout: float) -> FakeResponse:
        return FakeResponse(b" " * (MAX_RESPONSE_BYTES + 10))

    with pytest.raises(VenueRulesError, match="too large"):
        venue_rules._http_get("https://api.kraken.com/0/public/AssetPairs", opener=opener)


def test_http_get_wraps_network_errors() -> None:
    import urllib.error

    def opener(request: Any, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("unreachable")

    with pytest.raises(VenueRulesError, match="request failed"):
        venue_rules._http_get("https://api.kraken.com/0/public/AssetPairs", opener=opener)
