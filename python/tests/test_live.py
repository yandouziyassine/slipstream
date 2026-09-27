import json

from slipstream.live import LiveFeedError, root_cause, wall_clock
from slipstream.models import MarketDataError


def coinbase_sub_ack(seq: int) -> str:
    return json.dumps(
        {
            "channel": "subscriptions",
            "client_id": "",
            "timestamp": "t",
            "sequence_num": seq,
            "events": [{"subscriptions": {}}],
        }
    )


def test_root_cause_prefers_data_errors_over_disconnects() -> None:
    data_error = MarketDataError("unexpected symbol")
    group = ExceptionGroup("feeds", [ConnectionResetError("reset"), data_error])
    assert root_cause(group) is data_error


def test_root_cause_wraps_pure_network_failures() -> None:
    reset = ConnectionResetError("reset")
    cause = root_cause(ExceptionGroup("feeds", [reset]))
    assert isinstance(cause, LiveFeedError)
    assert cause.__cause__ is reset


def test_root_cause_keeps_live_feed_errors() -> None:
    idle = LiveFeedError("idle")
    assert root_cause(ExceptionGroup("feeds", [ConnectionResetError("x"), idle])) is idle


def test_wall_clock_never_goes_backwards() -> None:
    clock = wall_clock()
    readings = [clock() for _ in range(1000)]
    assert readings == sorted(readings)
