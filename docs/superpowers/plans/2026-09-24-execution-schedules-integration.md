# Execution Schedules — PR 3 (Integration) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- Wire the new engine schedules and the Python calibration together: `--algo`, `--urgency`, `--risk-aversion` and `--participation` on `live`/`replay`.
- Add a `compare` command that runs several algorithms side by side on the same feed.
- Forward trades to the engine.
- Add an end-to-end test that runs all four algorithms through the real engine against a fixture whose expected fills can be computed by hand.

**Architecture:**
- **Runner:** `ExecutionRunner` takes one or more `OrderSpec`s. On the first book snapshot it calibrates each spec into `ScheduleParams` (pure function in `calibration.py`) and submits it with those parameters. It forwards non-snapshot trade batches to the engine and reports done once every order is finished.
- **CLI:** loads calibration data before connecting to the engine, from the replay file's OHLC header lines or from live OHLC fetches, and only when VWAP or Almgren-Chriss is requested.

**Tech Stack:** Python 3.12, grpcio, pytest, mypy --strict, ruff; C++ engine built from `main` (PRs #7 and #8 merged).

**Spec:** `docs/superpowers/specs/2026-09-24-execution-schedules-design.md` §4.4–4.5, §5, §7.3.

**Branch/worktree:** `feat/schedules-integration` at `.worktrees/schedules-integration`, from `main` after PR #8 merges.

## Conventions
- **Python gate:** from `python/`, run `pytest -q`, `mypy slipstream`, `ruff check .` and `ruff format --check .`. On Windows use `C:/Users/yando/.venvs/slipstream/Scripts/python.exe -m …`; the end-to-end tests need the Linux engine binary, so they only pass in WSL.
- **Full gate:** in WSL, run `bash scripts/ci.sh` with the venv on PATH; it must print `CI OK`.
- **Commits:** use `GIT_AUTHOR_EMAIL=160782497+yandouziyassine@users.noreply.github.com GIT_COMMITTER_EMAIL=160782497+yandouziyassine@users.noreply.github.com git commit …` with a HEREDOC message that has a blank line before the `Co-Authored-By` trailer. Never change git config, and never push until Task I6.
- **Imports:** when adding tests to existing files, merge the new imports into the file's top import block.

---

### Task I1: Algorithm fields on `OrderSpec`, schedule parameter types, `schedule_params`

**Files:** Modify `python/slipstream/models.py` and `python/slipstream/calibration.py`; test `python/tests/test_calibration.py`.

- [ ] **Step 1: Append failing tests to `python/tests/test_calibration.py`**

```python
from slipstream.calibration import CalibrationData, schedule_params
from slipstream.models import (
    AlmgrenChrissParams,
    BookUpdate,
    OrderSpec,
    PovParams,
    TwapParams,
    VwapParams,
)

BOOK = BookUpdate(
    "BTC/USD",
    True,
    ((99.0, 1.0), (98.0, 1.0), (97.0, 2.0)),
    ((102.0, 2.0), (100.0, 1.0), (101.0, 1.0)),
)
ONE_MINUTE = tuple(bar(DAY0 + 60 * i, close=100.0 + (i % 2)) for i in range(61))
DATA = CalibrationData(bars_15m=tuple(history()), bars_1m=ONE_MINUTE)


def spec(algo: str, **kwargs: object) -> OrderSpec:
    return OrderSpec("o-1", "buy", 2.0, 4, 4, algo=algo, **kwargs)  # type: ignore[arg-type]


def test_twap_and_pov_need_no_calibration_data() -> None:
    assert schedule_params(spec("twap"), BOOK, DAY0 * NS, None) == TwapParams()
    assert schedule_params(spec("pov", participation=0.2), BOOK, DAY0 * NS, None) == PovParams(0.2)


def test_vwap_uses_time_of_day_weights() -> None:
    params = schedule_params(spec("vwap"), BOOK, DAY0 * NS, DATA)
    assert params == VwapParams((20.0, 20.0, 20.0, 20.0))


def test_almgren_chriss_calibrates_sigma_eta_and_urgency() -> None:
    params = schedule_params(spec("almgren_chriss", urgency="high"), BOOK, DAY0 * NS, DATA)
    assert isinstance(params, AlmgrenChrissParams)
    assert params.sigma == pytest.approx(math.sqrt(60 / 59) / math.sqrt(60))
    # asks are re-sorted best-first; slice 0.5, tau 1 s -> same cost curve as test_eta_from_book_cost_curve
    assert params.eta == pytest.approx(0.53125 / 1.796875 * 1.0)
    assert params.risk_aversion == 3e-4


def test_explicit_risk_aversion_overrides_urgency() -> None:
    params = schedule_params(spec("almgren_chriss", risk_aversion=7.5), BOOK, DAY0 * NS, DATA)
    assert isinstance(params, AlmgrenChrissParams)
    assert params.risk_aversion == 7.5


def test_sell_side_calibrates_against_bids() -> None:
    sell = OrderSpec("o-1", "sell", 2.0, 4, 4, algo="almgren_chriss")
    params = schedule_params(sell, BOOK, DAY0 * NS, DATA)
    assert isinstance(params, AlmgrenChrissParams)
    assert params.eta == pytest.approx(0.53125 / 1.796875 * 1.0)


@pytest.mark.parametrize("algo", ["vwap", "almgren_chriss"])
def test_calibrated_algos_require_data(algo: str) -> None:
    with pytest.raises(CalibrationError, match="calibration data"):
        schedule_params(spec(algo), BOOK, DAY0 * NS, None)
```

- [ ] **Step 2: Run the tests and confirm they fail** (import errors).

- [ ] **Step 3: Extend `python/slipstream/models.py`**
  - Add `Algo = Literal["twap", "vwap", "pov", "almgren_chriss"]` and `Urgency = Literal["low", "medium", "high"]` next to `Side`.
  - Replace `OrderSpec` with the version below. The defaults keep every existing five-argument construction valid.
```python
@dataclass(frozen=True)
class OrderSpec:
    order_id: str
    side: Side
    qty: float
    duration_s: int
    num_slices: int
    algo: Algo = "twap"
    urgency: Urgency = "medium"
    risk_aversion: float | None = None
    participation: float = 0.1
```
  - Append:
```python
@dataclass(frozen=True)
class TwapParams:
    pass


@dataclass(frozen=True)
class VwapParams:
    weights: tuple[float, ...]


@dataclass(frozen=True)
class AlmgrenChrissParams:
    sigma: float
    eta: float
    risk_aversion: float


@dataclass(frozen=True)
class PovParams:
    participation: float


ScheduleParams = TwapParams | VwapParams | AlmgrenChrissParams | PovParams
```

- [ ] **Step 4: Append to `python/slipstream/calibration.py`** (add `from dataclasses import dataclass` and the model imports)
```python
@dataclass(frozen=True)
class CalibrationData:
    bars_15m: tuple[Bar, ...]
    bars_1m: tuple[Bar, ...]


def schedule_params(
    spec: OrderSpec, book: BookUpdate, start_ns: int, data: CalibrationData | None
) -> ScheduleParams:
    if spec.algo == "twap":
        return TwapParams()
    if spec.algo == "pov":
        return PovParams(spec.participation)
    if data is None:
        raise CalibrationError(f"{spec.algo} needs OHLC calibration data")
    if spec.algo == "vwap":
        return VwapParams(vwap_weights(data.bars_15m, start_ns, spec.duration_s, spec.num_slices))
    liquidity = (
        sorted(book.asks) if spec.side == "buy" else sorted(book.bids, reverse=True)
    )
    tau_s = spec.duration_s / spec.num_slices
    eta = estimate_eta(liquidity, spec.qty / spec.num_slices, tau_s)
    sigma = estimate_sigma(data.bars_1m)
    risk_aversion = (
        spec.risk_aversion
        if spec.risk_aversion is not None
        else URGENCY_RISK_AVERSION[spec.urgency]
    )
    return AlmgrenChrissParams(sigma, eta, risk_aversion)
```
Imports needed: `from slipstream.models import AlmgrenChrissParams, BookUpdate, OrderSpec, PovParams, ScheduleParams, TwapParams, VwapParams`.

- [ ] **Step 5: Run the tests (pass), then the Python gate. Commit:** `feat(orchestrator): calibrate per-order schedule parameters`.

### Task I2: Engine client support for schedule parameters and trades

**Files:** Modify `python/slipstream/engine_client.py`; test `python/tests/test_engine_client.py`.

- [ ] **Step 1: Append failing tests**
```python
from slipstream.engine_client import trade_batch_to_proto
from slipstream.models import AlmgrenChrissParams, PovParams, TradeBatch, TwapParams, VwapParams


def test_order_defaults_to_twap_schedule() -> None:
    msg = order_to_proto(OrderSpec("o-1", "buy", 1.0, 4, 4), start_ns=0)
    assert msg.WhichOneof("schedule") == "twap"


def test_order_carries_schedule_params() -> None:
    spec = OrderSpec("o-1", "buy", 1.0, 4, 2)
    vwap = order_to_proto(spec, 0, VwapParams((1.0, 3.0)))
    assert vwap.WhichOneof("schedule") == "vwap"
    assert list(vwap.vwap.weights) == [1.0, 3.0]
    ac = order_to_proto(spec, 0, AlmgrenChrissParams(0.5, 2.0, 3e-5))
    assert ac.WhichOneof("schedule") == "almgren_chriss"
    assert (ac.almgren_chriss.sigma, ac.almgren_chriss.eta, ac.almgren_chriss.risk_aversion) == (
        0.5, 2.0, 3e-5)
    pov = order_to_proto(spec, 0, PovParams(0.25))
    assert pov.WhichOneof("schedule") == "pov"
    assert pov.pov.participation == 0.25
    assert order_to_proto(spec, 0, TwapParams()).WhichOneof("schedule") == "twap"


def test_trade_batch_to_proto() -> None:
    msg = trade_batch_to_proto(TradeBatch("BTC/USD", False, ((100.5, 0.2), (100.4, 0.3))))
    assert msg.symbol == "BTC/USD"
    assert [(t.price, t.qty) for t in msg.trades] == [(100.5, 0.2), (100.4, 0.3)]
```

- [ ] **Step 2: Run the tests and confirm they fail.**

- [ ] **Step 3: Implement in `python/slipstream/engine_client.py`**
  - Import `AlmgrenChrissParams`, `PovParams`, `ScheduleParams`, `TradeBatch` and `VwapParams` from `slipstream.models`.
  - Replace `order_to_proto` with:
```python
def order_to_proto(
    spec: OrderSpec, start_ns: int, params: ScheduleParams | None = None
) -> pb.ParentOrder:
    order = pb.ParentOrder(
        order_id=spec.order_id,
        side=_SIDES[spec.side],
        qty=spec.qty,
        start_ns=start_ns,
        duration_ns=spec.duration_s * _NS_PER_S,
        num_slices=spec.num_slices,
    )
    if isinstance(params, VwapParams):
        order.vwap.weights.extend(params.weights)
    elif isinstance(params, AlmgrenChrissParams):
        order.almgren_chriss.sigma = params.sigma
        order.almgren_chriss.eta = params.eta
        order.almgren_chriss.risk_aversion = params.risk_aversion
    elif isinstance(params, PovParams):
        order.pov.participation = params.participation
    else:
        order.twap.SetInParent()
    return order


def trade_batch_to_proto(batch: TradeBatch) -> pb.TradeBatch:
    return pb.TradeBatch(
        symbol=batch.symbol,
        trades=[pb.Trade(price=price, qty=qty) for price, qty in batch.trades],
    )
```
  - Change `EngineClient.submit` to accept `params: ScheduleParams | None = None` and pass it to `order_to_proto`.
  - Add:
```python
    def apply_trades(self, batch: TradeBatch) -> None:
        self._call(self._stub.ApplyTrades, trade_batch_to_proto(batch))
```

- [ ] **Step 4: Run the tests (pass) and the gate. Commit:** `feat(orchestrator): send schedule parameters and trades to the engine`.

### Task I3: Multi-order runner with trade forwarding; live trade subscription

**Files:** Modify `python/slipstream/runner.py`, `python/slipstream/live.py`, `python/tests/conftest.py`, `python/tests/test_runner.py` and `python/tests/test_live.py`.

- [ ] **Step 1: Update the `FakeEngine` in `python/tests/conftest.py`** so it records params and trades and reports every submitted order. Replace the class body's `submit` and `status`, and add `apply_trades` and the `trades` list:
```python
        self.trades: list[TradeBatch] = []
        self.submits: list[tuple[OrderSpec, int, ScheduleParams | None]] = []
```
```python
    def submit(
        self, spec: OrderSpec, start_ns: int, params: ScheduleParams | None = None
    ) -> tuple[bool, str]:
        self.submits.append((spec, start_ns, params))
        return self.accept, self.reason

    def apply_trades(self, batch: TradeBatch) -> None:
        self.trades.append(batch)

    def status(self) -> pb.StatusReply:
        if not self.accept:
            return pb.StatusReply()
        state = self.state
        if self.done_after_steps is not None and len(self.steps) >= self.done_after_steps:
            state = pb.ORDER_STATE_COMPLETED
        orders = [
            pb.OrderStatus(order_id=spec.order_id, state=state, algo=spec.algo)
            for spec, _, _ in self.submits
        ]
        return pb.StatusReply(orders=orders)
```
(Import `ScheduleParams` and `TradeBatch` from `slipstream.models`.)

- [ ] **Step 2: Update and add runner tests in `python/tests/test_runner.py`**
  - Change the existing assertion `assert fake_engine.submits == [(SPEC, 100)]` to `assert fake_engine.submits == [(SPEC, 100, TwapParams())]`.
  - Replace `test_trade_messages_do_not_touch_book_or_submit` with the tests below:
```python
def trade(msg_type: str = "update", symbol: str = "BTC/USD") -> str:
    return json.dumps({"channel": "trade", "type": msg_type,
                       "data": [{"symbol": symbol, "price": 100.0, "qty": 1.0}]})


def test_trade_updates_are_forwarded_but_snapshots_ignored(fake_engine: FakeEngine) -> None:
    runner = make_runner(fake_engine)
    runner.on_message(trade("snapshot"), 1)
    runner.on_message(trade(), 2)
    assert [batch.is_snapshot for batch in fake_engine.trades] == [False]
    assert fake_engine.books == []
    assert fake_engine.submits == []


def test_trade_symbol_mismatch_raises(fake_engine: FakeEngine) -> None:
    with pytest.raises(KrakenMessageError, match="symbol"):
        make_runner(fake_engine).on_message(trade(symbol="ETH/USD"), 1)


def test_submits_every_spec_with_calibrated_params(fake_engine: FakeEngine) -> None:
    specs = [
        OrderSpec("a", "buy", 1.0, 4, 4),
        OrderSpec("b", "buy", 1.0, 4, 4, algo="pov", participation=0.3),
    ]
    runner = ExecutionRunner(fake_engine, specs, "BTC/USD", logging.getLogger("test"))
    runner.on_message(snapshot(), 100)
    assert [(s.order_id, p) for s, _, p in fake_engine.submits] == [
        ("a", TwapParams()), ("b", PovParams(0.3))]
    assert [status.order_id for status in runner.order_statuses()] == ["a", "b"]


def test_done_only_when_every_order_is_terminal(fake_engine: FakeEngine) -> None:
    specs = [OrderSpec("a", "buy", 1.0, 4, 4), OrderSpec("b", "buy", 1.0, 4, 4)]
    runner = ExecutionRunner(fake_engine, specs, "BTC/USD", logging.getLogger("test"))
    assert not runner.is_done()
    runner.on_message(snapshot(), 100)
    assert not runner.is_done()
    fake_engine.state = pb.ORDER_STATE_COMPLETED
    assert runner.is_done()


def test_calibrated_algo_without_data_fails_before_submitting(fake_engine: FakeEngine) -> None:
    spec = OrderSpec("v", "buy", 1.0, 4, 4, algo="vwap")
    runner = ExecutionRunner(fake_engine, spec, "BTC/USD", logging.getLogger("test"))
    with pytest.raises(CalibrationError):
        runner.on_message(snapshot(), 100)
    assert fake_engine.submits == []
```
(Imports: `CalibrationError` from `slipstream.calibration`; `OrderSpec`, `PovParams` and `TwapParams` from `slipstream.models`.)

- [ ] **Step 3: Add to `python/tests/test_live.py`**, inside `test_streams_until_order_done` after the existing assertions: `assert received[0]["params"]["channel"] == "book"`. Also add a test that the server receives the trade subscription second:
```python
def test_subscribes_to_book_then_trades(fake_engine: FakeEngine) -> None:
    fake_engine.done_after_steps = 1
    received: list[dict[str, object]] = []

    async def handler(ws: ServerConnection) -> None:
        received.append(json.loads(await ws.recv()))
        received.append(json.loads(await ws.recv()))
        await ws.send(SNAPSHOT)
        await ws.wait_closed()

    async def scenario() -> None:
        async with serve(handler, "127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{port_of(server)}"
            await run_live(make_runner(fake_engine), "BTC/USD", 10, deadline_s=5, url=url)

    asyncio.run(scenario())
    assert [r["params"]["channel"] for r in received] == ["book", "trade"]  # type: ignore[index]
```

- [ ] **Step 4: Run the tests and confirm they fail.**

- [ ] **Step 5: Rewrite `python/slipstream/runner.py`**
```python
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from slipstream.calibration import CalibrationData, schedule_params
from slipstream.kraken import KrakenMessageError, parse_message
from slipstream.models import BookUpdate, Fill, OrderSpec, ScheduleParams, TradeBatch
from slipstream.v1 import execution_pb2 as pb

_TERMINAL_STATES = (pb.ORDER_STATE_COMPLETED, pb.ORDER_STATE_HALTED)


class Engine(Protocol):
    def apply_book(self, update: BookUpdate) -> None: ...
    def apply_trades(self, batch: TradeBatch) -> None: ...
    def submit(
        self, spec: OrderSpec, start_ns: int, params: ScheduleParams | None = None
    ) -> tuple[bool, str]: ...
    def step(self, now_ns: int) -> list[Fill]: ...
    def status(self) -> pb.StatusReply: ...


class OrderRejectedError(RuntimeError):
    pass


class ExecutionRunner:
    def __init__(
        self,
        engine: Engine,
        specs: OrderSpec | Sequence[OrderSpec],
        symbol: str,
        logger: logging.Logger,
        calibration: CalibrationData | None = None,
    ) -> None:
        self._engine = engine
        self._specs = (specs,) if isinstance(specs, OrderSpec) else tuple(specs)
        self._symbol = symbol
        self._log = logger
        self._calibration = calibration
        self._submitted = False
        self.fills: list[Fill] = []

    def on_message(self, raw: str | bytes, now_ns: int) -> None:
        update = parse_message(raw)
        if isinstance(update, BookUpdate):
            self._check_symbol(update.symbol)
            self._engine.apply_book(update)
            if update.is_snapshot and not self._submitted:
                self._submit_all(update, now_ns)
        elif isinstance(update, TradeBatch):
            self._check_symbol(update.symbol)
            if not update.is_snapshot:
                self._engine.apply_trades(update)
        if self._submitted:
            self._step(now_ns)

    def order_statuses(self) -> list[pb.OrderStatus]:
        by_id = {order.order_id: order for order in self._engine.status().orders}
        return [by_id[spec.order_id] for spec in self._specs if spec.order_id in by_id]

    def order_status(self) -> pb.OrderStatus | None:
        statuses = self.order_statuses()
        return statuses[0] if statuses else None

    def is_done(self) -> bool:
        statuses = self.order_statuses()
        return len(statuses) == len(self._specs) and all(
            status.state in _TERMINAL_STATES for status in statuses
        )

    def _check_symbol(self, symbol: str) -> None:
        if symbol != self._symbol:
            raise KrakenMessageError(f"unexpected symbol {symbol!r}")

    def _submit_all(self, book: BookUpdate, now_ns: int) -> None:
        planned = [(spec, schedule_params(spec, book, now_ns, self._calibration))
                   for spec in self._specs]
        for spec, params in planned:
            accepted, reason = self._engine.submit(spec, now_ns, params)
            if not accepted:
                raise OrderRejectedError(f"order {spec.order_id} rejected: {reason}")
            self._log.info(
                "order submitted",
                extra={
                    "fields": {
                        "event": "submit",
                        "order_id": spec.order_id,
                        "algo": spec.algo,
                        "side": spec.side,
                        "qty": spec.qty,
                        "duration_s": spec.duration_s,
                        "slices": spec.num_slices,
                        "params": repr(params),
                    }
                },
            )
        self._submitted = True

    def _step(self, now_ns: int) -> None:
        for fill in self._engine.step(now_ns):
            self.fills.append(fill)
            self._log.info(
                "fill",
                extra={
                    "fields": {
                        "event": "fill",
                        "order_id": fill.order_id,
                        "qty": fill.qty,
                        "price": fill.price,
                        "ts_ns": fill.ts_ns,
                    }
                },
            )
```
All specs are calibrated before any is submitted, so a calibration failure submits nothing.

- [ ] **Step 6: In `python/slipstream/live.py`**, import `subscribe_trades_message` and send it right after `subscribe_message(...)`.

- [ ] **Step 7: Run the tests (all pass) and the gate. Commit:** `feat(orchestrator): run several orders per session and forward trades to the engine`.

### Task I4: CLI `--algo` flags, calibration loading, `compare`

**Files:** Modify `python/slipstream/cli.py`; test `python/tests/test_cli.py`.

- [ ] **Step 1: Append failing tests to `python/tests/test_cli.py`**
```python
from slipstream.cli import format_comparison
from slipstream.models import Fill


def test_algo_flags_parse() -> None:
    args = build_parser().parse_args([*BASE, "--qty", "1", "--algo", "almgren_chriss",
                                      "--urgency", "high", "--risk-aversion", "0.5"])
    assert (args.algo, args.urgency, args.risk_aversion) == ("almgren_chriss", "high", 0.5)
    assert build_parser().parse_args([*BASE, "--qty", "1"]).algo == "twap"


@pytest.mark.parametrize("value", ["0", "0.51", "-0.1", "nan", "x"])
def test_participation_is_bounded(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([*BASE, "--qty", "1", "--participation", value])


def test_compare_parses_algo_list() -> None:
    args = build_parser().parse_args(
        ["compare", "--side", "buy", "--qty", "1", "--duration", "6", "--slices", "3",
         "--algos", "twap,pov,twap"])
    assert args.algos == ["twap", "pov"]
    assert args.file is None


@pytest.mark.parametrize("value", ["", "twap,bogus", ","])
def test_compare_rejects_bad_algo_lists(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["compare", "--side", "buy", "--qty", "1", "--duration", "6", "--slices", "3",
             "--algos", value])


def test_format_comparison_table() -> None:
    statuses = [
        pb.OrderStatus(order_id="a", algo="twap", state=pb.ORDER_STATE_COMPLETED,
                       filled_qty=1.0, avg_fill_price=101.0, slippage_bps=100.0,
                       immediate_cost_bps=150.0),
        pb.OrderStatus(order_id="b", algo="pov", state=pb.ORDER_STATE_HALTED,
                       filled_qty=0.5, avg_fill_price=101.0, slippage_bps=100.0,
                       immediate_cost_bps=150.0),
    ]
    fills = [Fill("a", 1, 0.5, 101.0), Fill("a", 2, 0.5, 101.0), Fill("b", 1, 0.5, 101.0)]
    lines = format_comparison(statuses, fills).splitlines()
    assert lines[0].split() == ["algo", "state", "filled", "avg", "px", "slip", "bps",
                                "1-shot", "bps", "saved", "bps", "fills"]
    assert lines[1].split() == ["twap", "COMPLETED", "1", "101.00", "100.00", "150.00", "50.00", "2"]
    assert lines[2].split() == ["pov", "HALTED", "0.5", "101.00", "100.00", "150.00", "50.00", "1"]


def test_summary_shows_algo() -> None:
    assert "algo         vwap" in format_summary(pb.OrderStatus(order_id="o", algo="vwap"))


def test_replay_without_ohlc_fails_before_connecting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SLIPSTREAM_PAPER_MODE", raising=False)
    file = tmp_path / "no-ohlc.jsonl"
    file.write_text(json.dumps({"recv_ns": 1, "msg": {"channel": "heartbeat"}}) + "\n",
                    encoding="utf-8")
    code = main(["replay", "--file", str(file), "--side", "buy", "--qty", "1",
                 "--duration", "6", "--slices", "3", "--algo", "vwap",
                 "--engine", "127.0.0.1:1"])
    assert code == 1
    assert "OHLC" in capsys.readouterr().err
```
(Add `import json` to the test file's imports.)

- [ ] **Step 2: Run the tests and confirm they fail.**

- [ ] **Step 3: Implement in `python/slipstream/cli.py`**
  - **New imports:** `from slipstream.calibration import CalibrationData, CalibrationError`, `from slipstream.kraken_rest import fetch_ohlc, parse_ohlc` (extend the existing import), `from slipstream.models import Fill, OrderSpec` (extend) and `from slipstream.replay import ReplayError, read_calibration, read_replay, run_replay` (extend).
  - **Constants:** `ALGOS = ("twap", "vwap", "pov", "almgren_chriss")` and `_CALIBRATED = ("vwap", "almgren_chriss")`.
  - **Argument types:**
```python
def _participation(value: str) -> float:
    result = _positive_float(value)
    if result > 0.5:
        raise argparse.ArgumentTypeError("participation must be at most 0.5")
    return result


def _algos(value: str) -> list[str]:
    names = [name.strip() for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in ALGOS]
    if not names or unknown:
        raise argparse.ArgumentTypeError(f"algos must be a comma list of {', '.join(ALGOS)}")
    return list(dict.fromkeys(names))
```
  - **Parser.** Update the description to `"Paper-trade execution schedules against the live or a recorded Kraken order book."`. Then:
    - Add `compare = commands.add_parser("compare", help="run several algorithms side by side on one feed")`, with `compare.add_argument("--algos", type=_algos, default=list(ALGOS))`, `compare.add_argument("--file", type=Path, default=None, help="replay file; live feed when omitted")` and `compare.add_argument("--depth", type=int, choices=[10, 25, 100], default=10)`.
    - Change the shared-args loop to `for sub in (live, replay, compare):`, and inside the loop add `sub.add_argument("--urgency", choices=["low", "medium", "high"], default="medium")`, `sub.add_argument("--risk-aversion", type=_positive_float, default=None)` and `sub.add_argument("--participation", type=_participation, default=0.1)`.
    - After the loop: `for sub in (live, replay): sub.add_argument("--algo", choices=ALGOS, default="twap")`.
  - **Formatters:**
    - In `format_summary`, insert `f"algo         {status.algo or 'twap'}",` as the second line.
    - Add:
```python
def format_comparison(statuses: Sequence[pb.OrderStatus], fills: Sequence[Fill]) -> str:
    rows = [
        f"{'algo':<16}{'state':<11}{'filled':>10}{'avg px':>12}{'slip bps':>10}"
        f"{'1-shot bps':>12}{'saved bps':>11}{'fills':>7}"
    ]
    for status in statuses:
        state = pb.OrderState.Name(status.state).removeprefix("ORDER_STATE_")
        count = sum(1 for fill in fills if fill.order_id == status.order_id)
        saved = status.immediate_cost_bps - status.slippage_bps
        rows.append(
            f"{status.algo:<16}{state:<11}{status.filled_qty:>10.8g}"
            f"{status.avg_fill_price:>12.2f}{status.slippage_bps:>10.2f}"
            f"{status.immediate_cost_bps:>12.2f}{saved:>11.2f}{count:>7}"
        )
    return "\n".join(rows)
```
  - **Order specs and calibration loading:**
```python
def _order_specs(args: argparse.Namespace) -> list[OrderSpec]:
    stamp = time.time_ns()
    algos = args.algos if args.command == "compare" else [args.algo]
    return [
        OrderSpec(
            order_id=(args.order_id if args.command != "compare" and args.order_id
                      else f"{'cmp-' if args.command == 'compare' else ''}{algo}-{stamp}"),
            side=args.side,
            qty=args.qty,
            duration_s=args.duration,
            num_slices=args.slices,
            algo=algo,
            urgency=args.urgency,
            risk_aversion=args.risk_aversion,
            participation=args.participation,
        )
        for algo in algos
    ]


def _load_calibration(args: argparse.Namespace, specs: Sequence[OrderSpec]) -> CalibrationData | None:
    if not any(spec.algo in _CALIBRATED for spec in specs):
        return None
    replay_file = getattr(args, "file", None)
    if replay_file is not None:
        bars = read_calibration(replay_file)
        if 15 not in bars or 1 not in bars:
            raise CalibrationError(
                "replay file has no OHLC calibration data; record it with `slipstream record`"
            )
        return CalibrationData(bars[15], bars[1])
    return CalibrationData(
        parse_ohlc(fetch_ohlc(args.symbol, 15)), parse_ohlc(fetch_ohlc(args.symbol, 1))
    )
```
  - **Replace the body of `main` after the record branch with:**
```python
    try:
        settings = load_settings()
        specs = _order_specs(args)
        calibration = _load_calibration(args, specs)
        client = EngineClient(args.engine or settings.engine_address)
    except (ConfigError, CalibrationError, KrakenMessageError, ReplayError, OSError) as exc:
        log.error(str(exc))
        return 1
    try:
        client.wait_ready()
        runner = ExecutionRunner(client, specs, args.symbol, log, calibration)
        replay_file = getattr(args, "file", None)
        if replay_file is not None:
            run_replay(runner, read_replay(replay_file))
        else:
            deadline_s = args.duration + _DEADLINE_GRACE_S
            asyncio.run(run_live(runner, args.symbol, args.depth, deadline_s=deadline_s))
        statuses = runner.order_statuses()
        if not statuses:
            log.error("order was never submitted (no order book snapshot received)")
            return 1
        if args.command == "compare":
            print(format_comparison(statuses, runner.fills))
        else:
            print(format_summary(statuses[0]))
        return 0
    except (
        EngineError,
        KrakenMessageError,
        ReplayError,
        OrderRejectedError,
        LiveFeedError,
        CalibrationError,
        OSError,
    ) as exc:
        log.error(str(exc))
        return 1
    finally:
        client.close()
```

- [ ] **Step 4: Run the tests (all pass) and the gate. Commit:** `feat(orchestrator): add --algo flags and a compare command`.

### Task I5: Four-algorithm end-to-end test against the real engine

**Files:** Modify `python/tests/conftest.py` (engine fixture `--max-position 0.1` → `1.0`, so four concurrent orders fit); create `python/tests/test_integration_schedules.py`.

The test builds its own replay file, so every expected number can be derived by hand:
- **Start:** `t0 = 00:14:58 UTC`, an order of `qty 0.04`, `duration 4 s`, `4 slices` (τ = 1 s). The slice midpoints fall at 00:14:58.5, 00:14:59.5, 00:15:00.5 and 00:15:01.5, so they straddle the 15-minute bucket boundary.
- **VWAP:** the 15-minute history puts volume 30 in bucket 0 and 10 in bucket 1, giving weights (30, 30, 10, 10). The fills are therefore 0.015, 0.015, 0.005, 0.005.
- **TWAP:** 0.01 per slice.
- **POV (p = 0.25):** market trades of 0.02, 0.06 and 0.08 at t0+0.5 s, +1.5 s and +2.5 s give cumulative volumes 0.02, 0.08 and 0.16. The targets are 0.005, 0.02 and 0.04, so the fills are 0.005, 0.015, 0.02.
- **Almgren-Chriss:** the test calibrates σ and η from the same data, then picks λ so that κτ = 0.5 exactly. The fill fractions are then `f_k = 1 − sinh(0.5(4−k))/sinh(2)`.

- [ ] **Step 1: Change `--max-position` in the `engine_address` fixture from `"0.1"` to `"1.0"`.**

- [ ] **Step 2: Write `python/tests/test_integration_schedules.py`**
```python
import json
import logging
import math
from pathlib import Path

import pytest

from slipstream.calibration import CalibrationData, estimate_eta, estimate_sigma
from slipstream.engine_client import EngineClient
from slipstream.kraken_rest import Bar
from slipstream.models import OrderSpec
from slipstream.replay import read_calibration, read_replay, run_replay
from slipstream.runner import ExecutionRunner
from slipstream.v1 import execution_pb2 as pb

DAY0 = 1_700_006_400
T0_S = DAY0 + 898
NS = 1_000_000_000
ASKS = [(100010.0, 0.01), (100020.0, 0.01), (100030.0, 0.02), (100040.0, 1.0)]
BIDS = [(99990.0, 1.0), (99980.0, 1.0)]


def ohlc_line(interval: int, rows: list[list[object]]) -> str:
    data = {"error": [], "result": {"XXBTZUSD": rows, "last": 0}}
    return json.dumps({"kind": "ohlc", "interval": interval, "data": data})


def row(time_s: int, close: float, volume: float) -> list[object]:
    c = f"{close:.1f}"
    return [time_s, c, c, c, c, c, f"{volume:.8f}", 1]


def msg_line(offset_s: float, msg: dict[str, object]) -> str:
    return json.dumps({"recv_ns": int((T0_S + offset_s) * NS), "msg": msg})


def book_snapshot() -> dict[str, object]:
    return {"channel": "book", "type": "snapshot", "data": [{
        "symbol": "BTC/USD",
        "bids": [{"price": p, "qty": q} for p, q in BIDS],
        "asks": [{"price": p, "qty": q} for p, q in ASKS]}]}


def trade(qty: float) -> dict[str, object]:
    return {"channel": "trade", "type": "update",
            "data": [{"symbol": "BTC/USD", "price": 100000.0, "qty": qty}]}


def build_session(path: Path) -> None:
    fifteen = [row(DAY0 - 86_400, 100000.0, 30.0), row(DAY0 - 86_400 + 900, 100000.0, 10.0)]
    one = [row(DAY0 + 60 * i, 100000.0 + (i % 2), 1.0) for i in range(61)]
    heartbeat = {"channel": "heartbeat"}
    lines = [
        ohlc_line(15, fifteen),
        ohlc_line(1, one),
        msg_line(0.0, {"method": "subscribe", "success": True, "result": {}}),
        msg_line(0.0, book_snapshot()),
        msg_line(0.5, trade(0.02)),
        msg_line(1.0, heartbeat),
        msg_line(1.5, trade(0.06)),
        msg_line(2.0, heartbeat),
        msg_line(2.5, trade(0.08)),
        msg_line(3.0, heartbeat),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fills_for(runner: ExecutionRunner, order_id: str) -> list[float]:
    return [fill.qty for fill in runner.fills if fill.order_id == order_id]


def test_four_schedules_end_to_end(engine_address: str, tmp_path: Path) -> None:
    session = tmp_path / "session.jsonl"
    build_session(session)
    bars = read_calibration(session)
    calibration = CalibrationData(bars[15], bars[1])

    sigma = estimate_sigma(calibration.bars_1m)
    eta = estimate_eta(ASKS, 0.01, 1.0)
    risk_aversion = (math.cosh(0.5) - 1.0) * 2.0 * eta / sigma**2

    specs = [
        OrderSpec("twap", "buy", 0.04, 4, 4, algo="twap"),
        OrderSpec("vwap", "buy", 0.04, 4, 4, algo="vwap"),
        OrderSpec("pov", "buy", 0.04, 4, 4, algo="pov", participation=0.25),
        OrderSpec("ac", "buy", 0.04, 4, 4, algo="almgren_chriss", risk_aversion=risk_aversion),
    ]
    client = EngineClient(engine_address)
    try:
        client.wait_ready()
        runner = ExecutionRunner(client, specs, "BTC/USD", logging.getLogger("test"), calibration)
        run_replay(runner, read_replay(session))
        statuses = {status.order_id: status for status in runner.order_statuses()}
    finally:
        client.close()

    assert {oid: pb.OrderState.Name(s.state) for oid, s in statuses.items()} == {
        oid: "ORDER_STATE_COMPLETED" for oid in ("twap", "vwap", "pov", "ac")}
    assert {oid: s.algo for oid, s in statuses.items()} == {
        "twap": "twap", "vwap": "vwap", "pov": "pov", "ac": "almgren_chriss"}
    assert fills_for(runner, "twap") == pytest.approx([0.01] * 4)
    assert fills_for(runner, "vwap") == pytest.approx([0.015, 0.015, 0.005, 0.005])
    assert fills_for(runner, "pov") == pytest.approx([0.005, 0.015, 0.02])
    cumulative = [1 - math.sinh(0.5 * (4 - k)) / math.sinh(2.0) for k in (1, 2, 3)] + [1.0]
    expected_ac = [0.04 * (b - a) for a, b in zip([0.0, *cumulative[:-1]], cumulative, strict=True)]
    assert fills_for(runner, "ac") == pytest.approx(expected_ac, abs=1e-9)
    assert fills_for(runner, "ac")[0] > 0.01
```

- [ ] **Step 3: Run it in WSL** (`bash scripts/ci.sh` with the venv on PATH). Expected: `CI OK`, with the new test passing. If a number disagrees, **stop and investigate the math** (calibration, slice timing, or engine release rules). Never adjust an expected value to match without explaining why the derivation was wrong.

- [ ] **Step 4: Commit:** `test: run TWAP, VWAP, POV and Almgren-Chriss end to end against the real engine`.

### Task I6: Live comparison run, docs, review, PR

**Files:** Create `scripts/demo_compare.sh`; modify `README.md` and `note.md`.

- [ ] **Step 1: Create `scripts/demo_compare.sh`**
```bash
#!/usr/bin/env bash
# Paper-trade the same order with every algorithm side by side on Kraken's live public BTC/USD book.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/python"
build/engine/slipstream_engine --listen 127.0.0.1:50051 \
  --max-order-notional 2000 --max-position 0.1 &
ENGINE_PID=$!
trap 'kill "$ENGINE_PID" 2>/dev/null || true' EXIT
python -m slipstream.cli compare --engine 127.0.0.1:50051 "$@"
```

- [ ] **Step 2: Run a real comparison in WSL.** It uses network access and public data only; fills are paper. Run it as a background command with a 30-minute timeout:
  `bash scripts/demo_compare.sh --side buy --qty 0.005 --duration 1200 --slices 20`
  (20 minutes, so the VWAP profile spans at least one 15-minute bucket boundary). Keep the comparison table.

- [ ] **Step 3: Update `README.md`**
  - Add a "Compare algorithms" section showing the command and the real table, with an honest reading of the result: which algorithm paid least, why, and why a single run proves nothing statistically (week 3 adds statistics).
  - Update the "How it works" bullets to mention the four schedules.
  - Move VWAP and Almgren-Chriss out of "Roadmap".

- [ ] **Step 4: Add a dev-log entry to `note.md`** (Day 2/3, week 2) covering:
  - the schedules and the calibration approach;
  - the redirect-refusal fix;
  - the κT presets;
  - the comparison result.

- [ ] **Step 5: Run `/security-review` on the branch.** Apply superpowers:receiving-code-review to the findings, then verification-before-completion, with `bash scripts/ci.sh` → `CI OK`.

- [ ] **Step 6: Commit** `scripts/demo_compare.sh README.md note.md` with the message `docs: add live algorithm comparison and week-2 dev log`. Push `feat/schedules-integration`, open PR 3, and merge once GitHub CI is green (standing approval).
