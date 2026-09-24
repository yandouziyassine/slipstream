from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from slipstream.kraken import KrakenMessageError

OHLC_URL = "https://api.kraken.com/0/public/OHLC"
MAX_RESPONSE_BYTES = 2 << 20
MAX_BARS = 1000
MAX_FIELD_CHARS = 64
SUPPORTED_INTERVALS = (1, 15)
_REST_PAIRS = {"BTC/USD": "XBTUSD", "ETH/USD": "ETHUSD"}


class KrakenRestError(KrakenMessageError):
    pass


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    # urllib follows redirects by default, including https -> http downgrades and other hosts.
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


NO_REDIRECT_OPENER = urllib.request.build_opener(_RefuseRedirects)


@dataclass(frozen=True)
class Bar:
    time_s: int
    open: float
    high: float
    low: float
    close: float
    vwap: float
    volume: float
    count: int


def rest_pair(symbol: str) -> str:
    try:
        return _REST_PAIRS[symbol]
    except KeyError as exc:
        raise KrakenRestError(f"no REST pair mapping for {symbol!r}") from exc


def fetch_ohlc(
    symbol: str,
    interval_min: int,
    opener: Callable[..., Any] = NO_REDIRECT_OPENER.open,
    timeout_s: float = 10.0,
) -> bytes:
    if interval_min not in SUPPORTED_INTERVALS:
        raise KrakenRestError(f"unsupported OHLC interval {interval_min}")
    query = urllib.parse.urlencode({"pair": rest_pair(symbol), "interval": interval_min})
    request = urllib.request.Request(  # noqa: S310 - fixed https URL, not user-controlled
        f"{OHLC_URL}?{query}", headers={"User-Agent": "slipstream"}
    )
    try:
        with opener(request, timeout=timeout_s) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise KrakenRestError(f"OHLC request failed: {exc}") from exc
    if not isinstance(body, bytes):
        raise KrakenRestError("OHLC response is not bytes")
    if len(body) > MAX_RESPONSE_BYTES:
        raise KrakenRestError("OHLC response too large")
    return body


def parse_ohlc(raw: str | bytes) -> tuple[Bar, ...]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise KrakenRestError("OHLC response too large")
    try:
        msg = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise KrakenRestError("invalid OHLC JSON") from exc
    if not isinstance(msg, dict):
        raise KrakenRestError("OHLC response is not an object")
    errors = msg.get("error")
    if not isinstance(errors, list):
        raise KrakenRestError("OHLC response missing error list")
    if errors:
        raise KrakenRestError(f"Kraken error: {errors[:3]!r}")
    result = msg.get("result")
    if not isinstance(result, dict):
        raise KrakenRestError("OHLC result is not an object")
    series = [value for key, value in result.items() if key != "last"]
    if len(series) != 1 or not isinstance(series[0], list):
        raise KrakenRestError("expected exactly one OHLC series")
    rows = series[0]
    if len(rows) > MAX_BARS:
        raise KrakenRestError("too many OHLC bars")
    return tuple(_parse_bar(row) for row in rows)


def _parse_bar(row: Any) -> Bar:
    if not isinstance(row, list) or len(row) != 8:
        raise KrakenRestError("OHLC row must have 8 fields")
    return Bar(
        time_s=_non_negative_int(row[0], "time"),
        open=_decimal(row[1], "open", allow_zero=False),
        high=_decimal(row[2], "high", allow_zero=False),
        low=_decimal(row[3], "low", allow_zero=False),
        close=_decimal(row[4], "close", allow_zero=False),
        vwap=_decimal(row[5], "vwap", allow_zero=True),
        volume=_decimal(row[6], "volume", allow_zero=True),
        count=_non_negative_int(row[7], "count"),
    )


def _decimal(value: Any, name: str, *, allow_zero: bool) -> float:
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARS:
        raise KrakenRestError(f"OHLC {name} must be a short decimal string")
    try:
        result = float(value)
    except ValueError as exc:
        raise KrakenRestError(f"OHLC {name} is not a number") from exc
    if not math.isfinite(result) or result < 0 or (result == 0 and not allow_zero):
        raise KrakenRestError(f"OHLC {name} out of range")
    return result


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise KrakenRestError(f"OHLC {name} must be a non-negative integer")
    return value
