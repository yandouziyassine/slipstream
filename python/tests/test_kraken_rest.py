import http.server
import inspect
import json
import threading
import urllib.error
from typing import Any

import pytest

from slipstream import kraken_rest
from slipstream.kraken import KrakenMessageError
from slipstream.kraken_rest import (
    MAX_RESPONSE_BYTES,
    Bar,
    KrakenRestError,
    fetch_ohlc,
    parse_ohlc,
    rest_pair,
)

ROWS = [
    [1700000000, "100.0", "101.0", "99.5", "100.5", "100.2", "2.5", 10],
    [1700000060, "100.5", "100.5", "100.5", "100.5", "0.0", "0.00000000", 0],
]


def ohlc_body(rows: Any = None, errors: Any = None, extra_series: bool = False) -> bytes:
    result: dict[str, Any] = {"XXBTZUSD": ROWS if rows is None else rows, "last": 1700000060}
    if extra_series:
        result["XETHZUSD"] = []
    return json.dumps({"error": [] if errors is None else errors, "result": result}).encode()


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def test_parses_bars() -> None:
    bars = parse_ohlc(ohlc_body())
    assert bars == (
        Bar(1700000000, 100.0, 101.0, 99.5, 100.5, 100.2, 2.5, 10),
        Bar(1700000060, 100.5, 100.5, 100.5, 100.5, 0.0, 0.0, 0),
    )


def test_kraken_error_array_raises() -> None:
    with pytest.raises(KrakenRestError, match="Kraken error"):
        parse_ohlc(ohlc_body(errors=["EQuery:Unknown asset pair"]))


def test_rest_errors_are_kraken_message_errors() -> None:
    assert issubclass(KrakenRestError, KrakenMessageError)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[" * 100_000,
        json.dumps([1]).encode(),
        json.dumps({"error": [], "result": []}).encode(),
        ohlc_body(extra_series=True),
        ohlc_body(rows=[[1700000000, "100.0"]]),
        ohlc_body(rows=[[True, "1", "1", "1", "1", "1", "1", 1]]),
        ohlc_body(rows=[[-1, "1", "1", "1", "1", "1", "1", 1]]),
        ohlc_body(rows=[[1, 1.0, "1", "1", "1", "1", "1", 1]]),
        ohlc_body(rows=[[1, "0", "1", "1", "1", "1", "1", 1]]),
        ohlc_body(rows=[[1, "nan", "1", "1", "1", "1", "1", 1]]),
        ohlc_body(rows=[[1, "1e400", "1", "1", "1", "1", "1", 1]]),
        ohlc_body(rows=[[1, "1" * 65, "1", "1", "1", "1", "1", 1]]),
        ohlc_body(rows=[[1, "1", "1", "1", "1", "1", "-1", 1]]),
        ohlc_body(rows=[[1, "1", "1", "1", "1", "1", "1", -1]]),
        ohlc_body(rows=[[1, "1", "1", "1", "1", "1", "1", 1]] * 1001),
    ],
    ids=[
        "not-json",
        "deep",
        "not-object",
        "result-not-object",
        "two-series",
        "short-row",
        "bool-time",
        "negative-time",
        "number-not-string",
        "zero-price",
        "nan",
        "overflow",
        "long-field",
        "negative-volume",
        "negative-count",
        "too-many-rows",
    ],
)
def test_hostile_ohlc_raises(raw: bytes) -> None:
    with pytest.raises(KrakenRestError):
        parse_ohlc(raw)


def test_rest_pair_mapping() -> None:
    assert rest_pair("BTC/USD") == "XBTUSD"
    with pytest.raises(KrakenRestError, match="no REST pair"):
        rest_pair("DOGE/XYZ")


def test_fetch_builds_fixed_https_request() -> None:
    seen: dict[str, Any] = {}

    def opener(request: Any, timeout: float) -> FakeResponse:
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return FakeResponse(ohlc_body())

    assert fetch_ohlc("BTC/USD", 15, opener=opener) == ohlc_body()
    assert seen == {
        "url": "https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=15",
        "timeout": 10.0,
    }


def test_fetch_rejects_oversized_response() -> None:
    def opener(request: Any, timeout: float) -> FakeResponse:
        return FakeResponse(b" " * (MAX_RESPONSE_BYTES + 10))

    with pytest.raises(KrakenRestError, match="too large"):
        fetch_ohlc("BTC/USD", 1, opener=opener)


def test_fetch_wraps_network_errors() -> None:
    def opener(request: Any, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("unreachable")

    with pytest.raises(KrakenRestError, match="request failed"):
        fetch_ohlc("BTC/USD", 1, opener=opener)


def test_fetch_rejects_unsupported_interval() -> None:
    with pytest.raises(KrakenRestError, match="interval"):
        fetch_ohlc("BTC/USD", 5)


def test_default_opener_refuses_redirects() -> None:
    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/downgraded")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return None

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        with pytest.raises(urllib.error.HTTPError, match="redirect"):
            kraken_rest.NO_REDIRECT_OPENER.open(url, timeout=5)
    finally:
        server.shutdown()
        server.server_close()
    assert inspect.signature(fetch_ohlc).parameters["opener"].default == (
        kraken_rest.NO_REDIRECT_OPENER.open
    )
