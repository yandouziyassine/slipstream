from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path

from slipstream.runner import ExecutionRunner


class ReplayError(ValueError):
    pass


def read_replay(path: Path) -> Iterator[tuple[int, str]]:
    with path.open(encoding="utf-8") as handle:
        lineno = 0
        while True:
            lineno += 1
            try:
                line = handle.readline()
            except UnicodeDecodeError as exc:
                raise ReplayError(f"line {lineno}: invalid UTF-8") from exc
            if line == "":
                return
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
                recv_ns = record["recv_ns"]
                msg = record["msg"]
            except (ValueError, RecursionError, KeyError, TypeError) as exc:
                raise ReplayError(f"line {lineno}: malformed replay record") from exc
            if isinstance(recv_ns, bool) or not isinstance(recv_ns, int) or recv_ns < 0:
                raise ReplayError(f"line {lineno}: recv_ns must be a non-negative integer")
            yield recv_ns, json.dumps(msg)


def run_replay(runner: ExecutionRunner, records: Iterable[tuple[int, str]]) -> None:
    last_ns = 0
    for recv_ns, raw in records:
        if recv_ns < last_ns:
            raise ReplayError("replay timestamps must be non-decreasing")
        last_ns = recv_ns
        runner.on_message(raw, recv_ns)
        if runner.is_done():
            return
