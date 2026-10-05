"""Starts one paper engine process on a loopback port picked by the OS, and always stops it."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

ENGINE_BINARY_NAME = "slipstream_engine"

_PORT_LINE = re.compile(rb"^slipstream engine listening on port (\d{1,5}) ", re.MULTILINE)
_PORT_SCAN_BYTES = 64 * 1024
_POLL_S = 0.05
_NUMBER = r"\d{1,12}(?:\.\d{1,12})?"
_VENUE_FLAG = re.compile(
    rf"(?:kraken|coinbase):fee_bps={_NUMBER}(?:,(?:min_qty|qty_step|min_notional)={_NUMBER})*"
)
_VENUE_FLAG_MAX_LEN = 160


class EngineConfigError(ValueError):
    pass


class EngineStartError(RuntimeError):
    pass


def validate_engine_binary(path: Path, build_dir: Path) -> Path:
    """The engine binary, resolved, once it is an executable slipstream_engine under build_dir."""
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise EngineConfigError(f"engine binary not found: {path}") from exc
    if not resolved.is_relative_to(build_dir.resolve()):
        raise EngineConfigError(f"engine binary must be inside {build_dir}")
    if not resolved.is_file():
        raise EngineConfigError(f"engine binary is not a file: {path}")
    if resolved.name != ENGINE_BINARY_NAME:
        raise EngineConfigError(f"engine binary must be named {ENGINE_BINARY_NAME}")
    if not os.access(resolved, os.X_OK):
        raise EngineConfigError(f"engine binary is not executable: {path}")
    return resolved


def validate_venue_flag(value: str) -> str:
    """One engine --venue value exactly as `slipstream.cli venue-flags` prints it."""
    if len(value) > _VENUE_FLAG_MAX_LEN or _VENUE_FLAG.fullmatch(value) is None:
        raise EngineConfigError(f"invalid engine --venue value {value[:40]!r}")
    return value


class RunningEngine:
    def __init__(self, process: subprocess.Popen[bytes], port: int) -> None:
        self._process = process
        self.address = f"127.0.0.1:{port}"

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        """None until the process has exited and been reaped."""
        return self._process.returncode


def _reported_port(log_path: Path) -> int | None:
    with log_path.open("rb") as handle:
        head = handle.read(_PORT_SCAN_BYTES)
    match = _PORT_LINE.search(head)
    if match is None:
        return None
    port = int(match.group(1))
    return port if 0 < port <= 65535 else None


async def _await_port(process: subprocess.Popen[bytes], log_path: Path, timeout_s: float) -> int:
    deadline = time.monotonic() + timeout_s
    while True:
        port = _reported_port(log_path)
        if port is not None:
            return port
        code = process.poll()
        if code is not None:
            raise EngineStartError(
                f"engine exited with code {code} before listening; see {log_path.name}"
            )
        if time.monotonic() >= deadline:
            raise EngineStartError(f"engine did not report its port within {timeout_s:g}s")
        await asyncio.sleep(_POLL_S)


def _stop(process: subprocess.Popen[bytes], timeout_s: float) -> None:
    # Synchronous on purpose: this also runs while the event loop is cancelling the hour (e.g. on
    # SIGTERM), where another await could be interrupted and leave the engine running.
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


@asynccontextmanager
async def running_engine(
    argv: Sequence[str],
    log_path: Path,
    startup_timeout_s: float = 10.0,
    stop_timeout_s: float = 5.0,
) -> AsyncIterator[RunningEngine]:
    """Runs the engine for the body of the `async with`, with its output in log_path."""
    try:
        with log_path.open("wb") as log_file:
            process = subprocess.Popen(  # noqa: S603 - validated argv, no shell
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
    except OSError as exc:
        raise EngineStartError(f"engine could not start: {exc}") from exc
    try:
        port = await _await_port(process, log_path, startup_timeout_s)
        yield RunningEngine(process, port)
    finally:
        _stop(process, stop_timeout_s)
