from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from slipstream.kraken import (
    KRAKEN_WS_URL,
    MAX_MESSAGE_BYTES,
    parse_message,
    subscribe_message,
    subscribe_trades_message,
)
from slipstream.kraken_rest import parse_ohlc

IDLE_TIMEOUT_S = 30.0


class RecordError(RuntimeError):
    pass


def open_new_file(path: Path) -> TextIO:
    try:
        return path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise RecordError(f"{path} already exists; refusing to overwrite") from exc


def write_ohlc_header(handle: TextIO, interval_min: int, raw: bytes) -> None:
    parse_ohlc(raw)
    record = {"kind": "ohlc", "interval": interval_min, "data": json.loads(raw)}
    handle.write(json.dumps(record) + "\n")


async def record_stream(
    handle: TextIO,
    symbol: str,
    depth: int,
    duration_s: float,
    url: str = KRAKEN_WS_URL,
    clock: Callable[[], int] = time.time_ns,
) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration_s
    written = 0
    try:
        async with connect(url, max_size=MAX_MESSAGE_BYTES, open_timeout=10) as ws:
            await ws.send(subscribe_message(symbol, depth))
            await ws.send(subscribe_trades_message(symbol))
            while (remaining := deadline - loop.time()) > 0:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(IDLE_TIMEOUT_S, remaining))
                except TimeoutError:
                    if loop.time() >= deadline:
                        break
                    raise RecordError("no market data received (idle timeout)") from None
                parse_message(raw)
                handle.write(json.dumps({"recv_ns": clock(), "msg": json.loads(raw)}) + "\n")
                written += 1
    except (WebSocketException, OSError) as exc:
        raise RecordError(f"market data connection failed: {exc}") from exc
    return written
