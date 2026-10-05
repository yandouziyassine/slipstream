import json

import pytest
from test_kraken import DOC_SNAPSHOT
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.http11 import Response

from slipstream.engine_stream import EngineError
from slipstream.kraken import KrakenMessageError
from slipstream.kraken_checksum import BookChecksumError
from slipstream.models import BookUpdate, MarketDataError
from slipstream.venue_ws import (
    FeedReconnect,
    IdleTimeoutError,
    ReconnectPolicy,
    ReconnectTracker,
    give_up_message,
    is_reconnectable,
    parser,
)


def test_kraken_parser_checks_book_checksums() -> None:
    parse = parser("kraken", "BTC/USD", 10)
    assert isinstance(parse(DOC_SNAPSHOT), BookUpdate)
    with pytest.raises(BookChecksumError, match="kraken BTC/USD"):
        parse(DOC_SNAPSHOT.replace('"qty":0.00100000', '"qty":0.00100001'))


def test_kraken_parser_requires_a_checksum() -> None:
    parse = parser("kraken", "BTC/USD", 10)
    with pytest.raises(KrakenMessageError, match="missing checksum"):
        parse(DOC_SNAPSHOT.replace(',"checksum":3310070434', ""))


def test_kraken_parser_is_fresh_per_connection() -> None:
    update = json.dumps(
        {
            "channel": "book",
            "type": "update",
            "data": [{"symbol": "BTC/USD", "bids": [], "asks": [], "checksum": 3310070434}],
        }
    )
    first = parser("kraken", "BTC/USD", 10)
    first(DOC_SNAPSHOT)
    assert isinstance(first(update), BookUpdate)
    with pytest.raises(KrakenMessageError, match="before the snapshot"):
        parser("kraken", "BTC/USD", 10)(update)


def test_backoff_doubles_from_half_a_second_and_caps_at_eight() -> None:
    policy = ReconnectPolicy()
    assert [policy.delay_s(n, lambda: 1.0) for n in range(1, 7)] == [0.5, 1, 2, 4, 8, 8]


def test_backoff_jitter_stays_within_the_upper_half() -> None:
    policy = ReconnectPolicy()
    assert policy.delay_s(3, lambda: 0.0) == 1.0
    assert policy.delay_s(3, lambda: 0.5) == 1.5


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_reconnects": -1},
        {"base_delay_s": 0.0},
        {"max_delay_s": float("nan")},
        {"base_delay_s": 2.0, "max_delay_s": 1.0},
    ],
)
def test_policy_rejects_nonsense(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        ReconnectPolicy(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("reset"),
        TimeoutError(),
        OSError("unreachable"),
        ConnectionClosedError(None, None),
        InvalidStatus(Response(503, "Unavailable", Headers())),
        IdleTimeoutError("no market data from kraken (idle timeout)"),
        BookChecksumError("kraken BTC/USD book checksum mismatch"),
    ],
)
def test_network_failures_and_checksum_mismatches_are_reconnectable(error: Exception) -> None:
    assert is_reconnectable(error)


@pytest.mark.parametrize(
    "error",
    [
        KrakenMessageError("invalid JSON"),
        MarketDataError("unexpected symbol"),
        EngineError("market stream failed"),
        ValueError("bug"),
    ],
)
def test_data_and_engine_errors_are_not_reconnectable(error: Exception) -> None:
    assert not is_reconnectable(error)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _tracker(
    reports: list[FeedReconnect], clock: _Clock, max_reconnects: int = 5
) -> ReconnectTracker:
    return ReconnectTracker(
        "kraken", ReconnectPolicy(max_reconnects=max_reconnects), reports.append, clock, lambda: 1.0
    )


SNAPSHOT = BookUpdate("BTC/USD", True, ((99.0, 1.0),), ((101.0, 1.0),))
DELTA = BookUpdate("BTC/USD", False, ((99.0, 2.0),), ())


def test_a_reconnect_is_reported_with_its_downtime_once_a_snapshot_arrives() -> None:
    reports: list[FeedReconnect] = []
    clock = _Clock()
    tracker = _tracker(reports, clock)
    assert tracker.failed(ConnectionResetError("reset by peer")) == 0.5
    clock.now = 101.0
    tracker.received(None)
    tracker.received(DELTA)
    assert reports == []
    clock.now = 102.5
    tracker.received(SNAPSHOT)
    assert reports == [
        FeedReconnect("kraken", 1, "ConnectionResetError: reset by peer", 2.5, True)
    ]
    tracker.received(SNAPSHOT)
    tracker.close()
    assert len(reports) == 1


def test_a_failure_before_recovery_settles_the_previous_attempt_unrecovered() -> None:
    reports: list[FeedReconnect] = []
    clock = _Clock()
    tracker = _tracker(reports, clock)
    tracker.failed(ConnectionResetError("first"))
    clock.now = 101.0
    assert tracker.failed(TimeoutError("second")) == 1.0
    assert reports == [FeedReconnect("kraken", 1, "ConnectionResetError: first", 1.0, False)]
    clock.now = 104.0
    tracker.close()
    assert reports[1] == FeedReconnect("kraken", 2, "TimeoutError: second", 3.0, False)


def test_the_failure_after_the_last_allowed_reconnect_gives_up() -> None:
    reports: list[FeedReconnect] = []
    clock = _Clock()
    tracker = _tracker(reports, clock, max_reconnects=2)
    assert tracker.failed(OSError("a")) is not None
    assert tracker.failed(OSError("b")) is not None
    assert tracker.failed(OSError("c")) is None
    assert [report.attempt for report in reports] == [1, 2]
    tracker.close()
    assert len(reports) == 2


def test_no_reconnects_allowed_means_the_first_failure_gives_up() -> None:
    reports: list[FeedReconnect] = []
    tracker = _tracker(reports, _Clock(), max_reconnects=0)
    assert tracker.failed(OSError("down")) is None
    assert reports == []


def test_reasons_are_bounded_and_printable() -> None:
    reports: list[FeedReconnect] = []
    tracker = _tracker(reports, _Clock())
    tracker.failed(OSError("bad\nline\x00" + "x" * 500))
    tracker.close()
    (report,) = reports
    assert len(report.reason) <= 200
    assert report.reason.isprintable()


def test_give_up_message_names_the_venue_cap_and_last_reason() -> None:
    message = give_up_message("coinbase", 5, IdleTimeoutError("no market data (idle timeout)"))
    assert message == (
        "coinbase feed gave up after 5 reconnects: IdleTimeoutError: no market data (idle timeout)"
    )
