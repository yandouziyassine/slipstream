from __future__ import annotations

import asyncio
import time

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from slipstream.kraken import KRAKEN_WS_URL, MAX_MESSAGE_BYTES, subscribe_message
from slipstream.runner import ExecutionRunner


class LiveFeedError(RuntimeError):
    pass


async def run_live(
    runner: ExecutionRunner,
    symbol: str,
    depth: int,
    deadline_s: float,
    idle_timeout_s: float = 30.0,
    url: str = KRAKEN_WS_URL,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    try:
        async with connect(url, max_size=MAX_MESSAGE_BYTES, open_timeout=10) as ws:
            await ws.send(subscribe_message(symbol, depth))
            while not runner.is_done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise LiveFeedError("order did not finish before the deadline")
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(idle_timeout_s, remaining))
                except TimeoutError as exc:
                    if loop.time() >= deadline:
                        raise LiveFeedError("order did not finish before the deadline") from exc
                    raise LiveFeedError("no market data received (idle timeout)") from exc
                runner.on_message(raw, time.time_ns())
    except (WebSocketException, OSError) as exc:
        raise LiveFeedError(f"market data connection failed: {exc}") from exc
