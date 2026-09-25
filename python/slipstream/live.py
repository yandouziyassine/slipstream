from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from slipstream.coinbase import COINBASE_WS_URL, subscribe_messages
from slipstream.coinbase import MAX_MESSAGE_BYTES as COINBASE_MAX_BYTES
from slipstream.kraken import (
    KRAKEN_WS_URL,
    MAX_MESSAGE_BYTES,
    subscribe_message,
    subscribe_trades_message,
)
from slipstream.models import Venue
from slipstream.runner import ExecutionRunner


class LiveFeedError(RuntimeError):
    pass


def _subscriptions(venue: Venue, symbol: str, depth: int) -> list[str]:
    if venue == "coinbase":
        return subscribe_messages(symbol)
    return [subscribe_message(symbol, depth), subscribe_trades_message(symbol)]


async def run_live(
    runner: ExecutionRunner,
    symbol: str,
    depth: int,
    deadline_s: float,
    idle_timeout_s: float = 30.0,
    venues: Sequence[Venue] = ("kraken",),
    urls: Mapping[Venue, str] | None = None,
) -> None:
    endpoints: dict[Venue, str] = {"kraken": KRAKEN_WS_URL, "coinbase": COINBASE_WS_URL}
    endpoints.update(urls or {})
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    finished = asyncio.Event()

    async def feed(venue: Venue) -> None:
        max_size = COINBASE_MAX_BYTES if venue == "coinbase" else MAX_MESSAGE_BYTES
        async with connect(endpoints[venue], max_size=max_size, open_timeout=10) as ws:
            for message in _subscriptions(venue, symbol, depth):
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
                runner.on_message(raw, time.time_ns(), venue)
                if runner.is_done():
                    finished.set()

    try:
        async with asyncio.TaskGroup() as group:
            for venue in venues:
                group.create_task(feed(venue))
    except ExceptionGroup as errors:
        first = errors.exceptions[0]
        if isinstance(first, (WebSocketException, OSError)):
            raise LiveFeedError(f"market data connection failed: {first}") from first
        raise first from None
