from __future__ import annotations

import time
from collections.abc import Callable

from websockets.exceptions import WebSocketException


class LiveFeedError(RuntimeError):
    pass


def wall_clock() -> Callable[[], int]:
    """Wall-clock nanoseconds that never go backwards: anchored once, advanced monotonically."""
    wall_start = time.time_ns()
    mono_start = time.monotonic_ns()
    return lambda: wall_start + (time.monotonic_ns() - mono_start)


def root_cause(
    errors: ExceptionGroup[Exception], feed_error: type[Exception] = LiveFeedError
) -> Exception:
    """Pick the error that explains a multi-feed failure: data/engine errors beat disconnects."""
    network = (WebSocketException, OSError)
    for error in errors.exceptions:
        if not isinstance(error, (*network, feed_error)):
            return error
    for error in errors.exceptions:
        if isinstance(error, feed_error):
            return error
    first = errors.exceptions[0]
    wrapped = feed_error(f"market data connection failed: {first}")
    wrapped.__cause__ = first
    return wrapped
