from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from slipstream.engine_process import (
    EngineConfigError,
    EngineStartError,
    RunningEngine,
    running_engine,
    validate_engine_binary,
    validate_venue_flag,
)

PORT_LINE = "slipstream engine listening on port {port} (paper mode, symbol BTC/USD, venues)"


def _fake_engine(tmp_path: Path, body: str, name: str = "slipstream_engine") -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)
    return path


def _listening_engine(tmp_path: Path, port: int = 4242) -> Path:
    return _fake_engine(tmp_path, f'echo "{PORT_LINE.format(port=port)}"\nexec sleep 60\n')


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_running_engine_yields_the_reported_port_and_stops_the_engine(tmp_path: Path) -> None:
    binary = _listening_engine(tmp_path)
    log_path = tmp_path / "engine.log"

    async def scenario() -> RunningEngine:
        async with running_engine([str(binary)], log_path) as engine:
            assert engine.address == "127.0.0.1:4242"
            assert engine.returncode is None
            assert not _gone(engine.pid)
        return engine

    engine = asyncio.run(scenario())

    assert engine.returncode is not None
    assert _gone(engine.pid)
    assert "listening on port 4242" in log_path.read_text(encoding="utf-8")


def test_running_engine_stops_the_engine_when_the_run_fails(tmp_path: Path) -> None:
    binary = _listening_engine(tmp_path)
    seen: list[RunningEngine] = []

    async def scenario() -> None:
        async with running_engine([str(binary)], tmp_path / "engine.log") as engine:
            seen.append(engine)
            raise RuntimeError("run failed")

    with pytest.raises(RuntimeError, match="run failed"):
        asyncio.run(scenario())

    assert seen[0].returncode is not None
    assert _gone(seen[0].pid)


def test_running_engine_reports_an_engine_that_exits_before_listening(tmp_path: Path) -> None:
    binary = _fake_engine(tmp_path, 'echo "error: invalid --venue" >&2\nexit 2\n')
    log_path = tmp_path / "engine.log"

    async def scenario() -> None:
        async with running_engine([str(binary)], log_path):
            pytest.fail("a dead engine must not be yielded")

    with pytest.raises(EngineStartError, match="exited with code 2"):
        asyncio.run(scenario())
    assert "invalid --venue" in log_path.read_text(encoding="utf-8")


def test_running_engine_kills_an_engine_that_never_reports_its_port(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    binary = _fake_engine(tmp_path, f'echo $$ > "{pid_file}"\nexec sleep 60\n')

    async def scenario() -> None:
        async with running_engine([str(binary)], tmp_path / "engine.log", startup_timeout_s=0.5):
            pytest.fail("an engine without a port must not be yielded")

    with pytest.raises(EngineStartError, match="did not report its port"):
        asyncio.run(scenario())
    assert _gone(int(pid_file.read_text(encoding="utf-8")))


def test_running_engine_kills_an_engine_that_ignores_sigterm(tmp_path: Path) -> None:
    binary = _fake_engine(
        tmp_path,
        f"""\
        trap '' TERM
        echo "{PORT_LINE.format(port=4243)}"
        while :; do sleep 0.05; done
        """,
    )

    async def scenario() -> RunningEngine:
        async with running_engine(
            [str(binary)], tmp_path / "engine.log", stop_timeout_s=0.3
        ) as engine:
            return engine

    started = time.monotonic()
    engine = asyncio.run(scenario())

    assert engine.returncode == -signal.SIGKILL
    assert time.monotonic() - started < 5


def test_running_engine_rejects_a_binary_that_cannot_be_executed(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_engine([str(tmp_path / "missing")], tmp_path / "engine.log"):
            pytest.fail("a missing binary must not be yielded")

    with pytest.raises(EngineStartError, match="could not start"):
        asyncio.run(scenario())


def test_running_engine_ignores_an_out_of_range_port(tmp_path: Path) -> None:
    binary = _listening_engine(tmp_path, port=70000)

    async def scenario() -> None:
        async with running_engine([str(binary)], tmp_path / "engine.log", startup_timeout_s=0.5):
            pytest.fail("an invalid port must not be yielded")

    with pytest.raises(EngineStartError, match="did not report its port"):
        asyncio.run(scenario())


def test_sigterm_to_the_collector_still_stops_its_engine(tmp_path: Path) -> None:
    binary = _listening_engine(tmp_path)
    pid_file = tmp_path / "engine.pid"
    child = textwrap.dedent(
        f"""\
        import asyncio
        from pathlib import Path
        from slipstream.collect import sigterm_exits
        from slipstream.engine_process import running_engine

        async def main() -> None:
            async with running_engine([{str(binary)!r}], Path({str(tmp_path / "e.log")!r})) as e:
                Path({str(pid_file)!r}).write_text(str(e.pid))
                await asyncio.sleep(60)

        with sigterm_exits():
            asyncio.run(main())
        """
    )
    python_dir = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-c", child], cwd=python_dir, env={**os.environ, "PYTHONPATH": "."}
    )
    try:
        deadline = time.monotonic() + 20
        while not pid_file.exists() or not pid_file.read_text(encoding="utf-8"):
            assert proc.poll() is None, "collector child exited early"
            assert time.monotonic() < deadline, "engine never started"
            time.sleep(0.05)
        engine_pid = int(pid_file.read_text(encoding="utf-8"))
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 128 + signal.SIGTERM
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert _gone(engine_pid)


def test_validate_engine_binary_accepts_an_executable_inside_the_build_dir(
    tmp_path: Path,
) -> None:
    build_dir = tmp_path / "build"
    (build_dir / "release").mkdir(parents=True)
    binary = _listening_engine(build_dir / "release")

    assert validate_engine_binary(binary, build_dir) == binary.resolve()


def test_validate_engine_binary_rejects_a_missing_file(tmp_path: Path) -> None:
    (tmp_path / "build").mkdir()
    with pytest.raises(EngineConfigError, match="not found"):
        validate_engine_binary(tmp_path / "build" / "slipstream_engine", tmp_path / "build")


def test_validate_engine_binary_rejects_a_binary_outside_the_build_dir(tmp_path: Path) -> None:
    (tmp_path / "build").mkdir()
    binary = _listening_engine(tmp_path)
    with pytest.raises(EngineConfigError, match="inside"):
        validate_engine_binary(binary, tmp_path / "build")


def test_validate_engine_binary_rejects_a_symlink_escaping_the_build_dir(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (build_dir / "slipstream_engine").symlink_to(_listening_engine(outside))
    with pytest.raises(EngineConfigError, match="inside"):
        validate_engine_binary(build_dir / "slipstream_engine", build_dir)


def test_validate_engine_binary_rejects_a_directory(tmp_path: Path) -> None:
    (tmp_path / "build" / "slipstream_engine").mkdir(parents=True)
    with pytest.raises(EngineConfigError, match="not a file"):
        validate_engine_binary(tmp_path / "build" / "slipstream_engine", tmp_path / "build")


def test_validate_engine_binary_rejects_another_program(tmp_path: Path) -> None:
    (tmp_path / "build").mkdir()
    other = _fake_engine(tmp_path / "build", "exit 0\n", name="sh")
    with pytest.raises(EngineConfigError, match="slipstream_engine"):
        validate_engine_binary(other, tmp_path / "build")


def test_validate_engine_binary_rejects_a_non_executable_file(tmp_path: Path) -> None:
    (tmp_path / "build").mkdir()
    binary = _listening_engine(tmp_path / "build")
    binary.chmod(0o644)
    with pytest.raises(EngineConfigError, match="executable"):
        validate_engine_binary(binary, tmp_path / "build")


@pytest.mark.parametrize(
    "flag",
    [
        "kraken:fee_bps=40",
        "kraken:fee_bps=40,min_qty=0.00005,qty_step=0.00000001,min_notional=0.5",
        "coinbase:fee_bps=60,min_qty=0.00000001,qty_step=0.00000001,min_notional=1",
    ],
)
def test_validate_venue_flag_accepts_venue_flags_output(flag: str) -> None:
    assert validate_venue_flag(flag) == flag


@pytest.mark.parametrize(
    "flag",
    [
        "",
        "--clock",
        "replay",
        "binance:fee_bps=10",
        "kraken",
        "kraken:min_qty=1",
        "kraken:fee_bps=40,min_qty=1;rm -rf /",
        "kraken:fee_bps=40,unknown=1",
        "kraken:fee_bps=-1",
        "kraken:fee_bps=nan",
        "kraken:fee_bps=0,min_qty=5e-05",
        "kraken:fee_bps=40\n--clock",
        "kraken:fee_bps=" + "1" * 300,
    ],
)
def test_validate_venue_flag_rejects_anything_else(flag: str) -> None:
    with pytest.raises(EngineConfigError):
        validate_venue_flag(flag)
