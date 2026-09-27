from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from slipstream.models import Venue
from slipstream.runner import ExecutionRunner
from slipstream.venue_ws import IDLE_TIMEOUT_S, endpoints, max_message_bytes, subscriptions


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


async def run_live(
    runner: ExecutionRunner,
    symbol: str,
    depth: int,
    deadline_s: float,
    idle_timeout_s: float = IDLE_TIMEOUT_S,
    venues: Sequence[Venue] = ("kraken",),
    urls: Mapping[Venue, str] | None = None,
) -> None:
    urls_by_venue = endpoints(urls)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    finished = asyncio.Event()
    clock = wall_clock()
    tasks: list[asyncio.Task[None]] = []

    async def feed(venue: Venue) -> None:
        max_size = max_message_bytes(venue)
        async with connect(urls_by_venue[venue], max_size=max_size, open_timeout=10) as ws:
            for message in subscriptions(venue, symbol, depth):
                await ws.send(message)
            while not finished.is_set():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise LiveFeedError("order did not finish before the deadline")
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(idle_timeout_s, remaining))
                except TimeoutError as exc:
                    if loop.time() >= deadline:
                        raise LiveFeedError("order did not finish before the deadline") from exc
                    raise LiveFeedError(f"no market data from {venue} (idle timeout)") from exc
                runner.on_message(raw, clock(), venue)
                if runner.is_done():
                    finished.set()
                    current = asyncio.current_task()
                    for task in tasks:
                        if task is not current:
                            task.cancel()

    try:
        async with asyncio.TaskGroup() as group:
            for venue in venues:
                tasks.append(group.create_task(feed(venue)))
    except ExceptionGroup as errors:
        raise root_cause(errors) from errors
