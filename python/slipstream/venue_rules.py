from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from slipstream.kraken_rest import NO_REDIRECT_OPENER
from slipstream.models import MarketDataError, Venue

KRAKEN_ASSET_PAIRS_URL = "https://api.kraken.com/0/public/AssetPairs"
COINBASE_PRODUCT_URL = "https://api.coinbase.com/api/v3/brokerage/market/products"
MAX_RESPONSE_BYTES = 1 << 20
MAX_FIELD_CHARS = 64
MAX_LOT_DECIMALS = 12

# symbol -> (Kraken REST query pair, Kraken result key)
_KRAKEN_PAIRS: dict[str, tuple[str, str]] = {"BTC/USD": ("XBTUSD", "XXBTZUSD")}
_COINBASE_PRODUCTS: dict[str, str] = {"BTC/USD": "BTC-USD"}


class VenueRulesError(MarketDataError):
    pass


@dataclass(frozen=True)
class VenueRules:
    min_qty: float
    qty_step: float
    min_notional: float


def parse_kraken_rules(raw: bytes, pair_key: str = "XXBTZUSD") -> VenueRules:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise VenueRulesError("Kraken AssetPairs response too large")
    try:
        msg = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise VenueRulesError("invalid Kraken AssetPairs JSON") from exc
    if not isinstance(msg, dict):
        raise VenueRulesError("Kraken AssetPairs response is not an object")
    errors = msg.get("error")
    if not isinstance(errors, list):
        raise VenueRulesError("Kraken AssetPairs response missing error list")
    if errors:
        raise VenueRulesError(f"Kraken error: {errors[:3]!r}")
    result = msg.get("result")
    if not isinstance(result, dict):
        raise VenueRulesError("Kraken AssetPairs result is not an object")
    if list(result) != [pair_key]:
        raise VenueRulesError(f"expected exactly one AssetPairs entry keyed {pair_key!r}")
    pair = result[pair_key]
    if not isinstance(pair, dict):
        raise VenueRulesError("Kraken AssetPairs entry is not an object")
    return VenueRules(
        min_qty=_decimal(pair.get("ordermin"), "ordermin"),
        qty_step=_lot_step(pair.get("lot_decimals")),
        min_notional=_decimal(pair.get("costmin"), "costmin"),
    )


def parse_coinbase_rules(raw: bytes, product: str = "BTC-USD") -> VenueRules:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise VenueRulesError("Coinbase product response too large")
    try:
        msg = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise VenueRulesError("invalid Coinbase product JSON") from exc
    if not isinstance(msg, dict):
        raise VenueRulesError("Coinbase product response is not an object")
    if msg.get("product_id") != product:
        raise VenueRulesError("unexpected Coinbase product_id")
    if msg.get("status") != "online" or msg.get("trading_disabled") is not False:
        raise VenueRulesError("venue not trading")
    return VenueRules(
        min_qty=_decimal(msg.get("base_min_size"), "base_min_size"),
        qty_step=_decimal(msg.get("base_increment"), "base_increment"),
        min_notional=_decimal(msg.get("quote_min_size"), "quote_min_size"),
    )


def _http_get(
    url: str,
    opener: Callable[..., Any] = NO_REDIRECT_OPENER.open,
    timeout_s: float = 10.0,
) -> bytes:
    request = urllib.request.Request(  # noqa: S310 - fixed https URL, not user-controlled
        url, headers={"User-Agent": "slipstream"}
    )
    try:
        with opener(request, timeout=timeout_s) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise VenueRulesError(f"venue rules request failed: {exc}") from exc
    if not isinstance(body, bytes):
        raise VenueRulesError("venue rules response is not bytes")
    if len(body) > MAX_RESPONSE_BYTES:
        raise VenueRulesError("venue rules response too large")
    return body


def fetch_venue_rules(
    venues: Sequence[Venue], symbol: str, fetch: Callable[[str], bytes] = _http_get
) -> dict[Venue, VenueRules]:
    if symbol not in _KRAKEN_PAIRS or symbol not in _COINBASE_PRODUCTS:
        raise VenueRulesError(f"no venue rules mapping for {symbol!r}")
    kraken_query, kraken_result_key = _KRAKEN_PAIRS[symbol]
    coinbase_product = _COINBASE_PRODUCTS[symbol]
    rules: dict[Venue, VenueRules] = {}
    for venue in venues:
        if venue == "kraken":
            url = f"{KRAKEN_ASSET_PAIRS_URL}?pair={kraken_query}"
            rules["kraken"] = parse_kraken_rules(fetch(url), pair_key=kraken_result_key)
        elif venue == "coinbase":
            url = f"{COINBASE_PRODUCT_URL}/{coinbase_product}"
            rules["coinbase"] = parse_coinbase_rules(fetch(url), product=coinbase_product)
        else:
            raise VenueRulesError(f"unsupported venue {venue!r}")
    return rules


def _decimal(value: Any, name: str) -> float:
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARS:
        raise VenueRulesError(f"{name} must be a short decimal string")
    try:
        result = float(value)
    except ValueError as exc:
        raise VenueRulesError(f"{name} is not a number") from exc
    if not math.isfinite(result) or result < 0:
        raise VenueRulesError(f"{name} out of range")
    return result


def _lot_step(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_LOT_DECIMALS:
        raise VenueRulesError("lot_decimals must be an integer between 0 and 12")
    return 10.0**-value
