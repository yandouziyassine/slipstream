from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]
Algo = Literal["twap", "vwap", "pov", "almgren_chriss"]
Urgency = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class BookUpdate:
    symbol: str
    is_snapshot: bool
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class TradeBatch:
    symbol: str
    is_snapshot: bool
    trades: tuple[tuple[float, float], ...]


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


@dataclass(frozen=True)
class Fill:
    order_id: str
    ts_ns: int
    qty: float
    price: float


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
